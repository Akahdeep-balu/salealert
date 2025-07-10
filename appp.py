import os
from flask import Flask, request, jsonify
from flask_cors import CORS, cross_origin
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
import uuid
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
# Load env vars
load_dotenv()

app = Flask(__name__)

CORS(app, origins="*")

DATABASE_URL = os.getenv('DATABASE_URL')

# Twilio config
twilio_sid = os.getenv('TWILIO_ACCOUNT_SID')
twilio_token = os.getenv('TWILIO_AUTH_TOKEN')
twilio_phone = os.getenv('TWILIO_PHONE_NUMBER')
twilio_client = Client(twilio_sid, twilio_token)

# AWS S3 config
AWS_ACCESS_KEY = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_BUCKET = os.getenv('AWS_S3_BUCKET')
AWS_REGION = os.getenv('AWS_REGION')
print(AWS_ACCESS_KEY,AWS_SECRET_KEY,AWS_BUCKET,AWS_REGION)
s3_client = boto3.client('s3',
                         aws_access_key_id=AWS_ACCESS_KEY,
                         aws_secret_access_key=AWS_SECRET_KEY,
                         region_name=AWS_REGION)

scheduler = BackgroundScheduler()
scheduler.start()

# --- DB Setup ---

def create_connection():
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        return conn
    except Exception as e:
        print(f"DB connection error: {e}")
        return None

def create_table():
    conn = create_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS signups (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    phone TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS schedules (
                    id SERIAL PRIMARY KEY,
                    flyer_url TEXT NOT NULL,
                    scheduled_time TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')
            conn.commit()
        except Exception as e:
            print(f"Table creation error: {e}")
        finally:
            conn.close()

create_table()

# --- Twilio SMS Sender ---

def send_sms(to_number, message_body):
    try:
        message = twilio_client.messages.create(
            body=message_body,
            from_=twilio_phone,
            to=to_number
        )
        print(f"SMS sent to {to_number}: SID {message.sid}")
        return True
    except Exception as e:
        print(f"Twilio error: {e}")
        return False

# --- S3 Upload ---

def upload_to_s3(file_obj, filename):
    try:
        s3_key = f"flyers/{uuid.uuid4()}_{filename}"
        s3_client.upload_fileobj(file_obj, AWS_BUCKET, s3_key)
        url = f"https://{AWS_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
        return url
    except ClientError as e:
        print(f"S3 Upload error: {e}")
        return None

# --- API Routes ---
@app.route('/test_cors')
def test_cors():
    return 'CORS works!'

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json()
    name = data.get('name', '').strip()
    phone = data.get('phone', '').strip()

    if not phone:
        return jsonify({'error': 'Phone number is required'}), 400

    conn = create_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM signups WHERE phone = %s", (phone,))
        if cursor.fetchone():
            return jsonify({'error': 'Phone already subscribed'}), 409

        cursor.execute("INSERT INTO signups (name, phone) VALUES (%s, %s)", (name, phone))
        conn.commit()
        return jsonify({'success': True}), 200
    except Exception as e:
        print(f"DB error: {e}")
        return jsonify({'error': 'Database error'}), 500
    finally:
        conn.close()

@app.route('/api/recipients')
def get_recipients():
    conn = create_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name, phone, created_at FROM signups ORDER BY created_at DESC")
    users = cursor.fetchall()
    conn.close()

    result = [{'name': u[0], 'phone': u[1], 'created_at': u[2]} for u in users]
    return jsonify(result)

@app.route('/api/upload_flyer', methods=['POST'])
def upload_flyer():
    print("Upload flyer endpoint hit")  # << add this
    if 'flyer' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    flyer = request.files['flyer']

    if flyer.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if not (flyer.filename.endswith('.png') or flyer.filename.endswith('.pdf')):
        return jsonify({'error': 'Invalid file type. Only PNG and PDF allowed.'}), 400

    flyer_url = upload_to_s3(flyer, flyer.filename)
    if flyer_url:
        return jsonify({'flyer_url': flyer_url}), 200
    else:
        return jsonify({'error': 'Failed to upload flyer'}), 500

@app.route('/api/schedule', methods=['POST'])
def schedule_sms():
    data = request.get_json()
    scheduled_time_str = data.get('scheduled_time')
    flyer_url = data.get('flyer_url')

    if not scheduled_time_str or not flyer_url:
        return jsonify({'error': 'Missing scheduled_time or flyer_url'}), 400

    try:
        scheduled_time = datetime.fromisoformat(scheduled_time_str)
    except Exception:
        return jsonify({'error': 'Invalid date format'}), 400

    if scheduled_time < datetime.now():
        return jsonify({'error': 'Scheduled time must be in the future'}), 400

    conn = create_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO schedules (flyer_url, scheduled_time) VALUES (%s, %s)",
        (flyer_url, scheduled_time_str)
    )
    conn.commit()
    conn.close()

    # Schedule the SMS sending job
    scheduler.add_job(func=send_scheduled_sms, trigger='date', run_date=scheduled_time, args=[flyer_url])

    return jsonify({'success': True}), 200

def send_scheduled_sms(flyer_url):
    print(f"Sending scheduled SMS for flyer: {flyer_url}")
    conn = create_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM signups")
    users = cursor.fetchall()
    conn.close()

    message_body = f"New Weekly Flyer from New India Bazar! Check it out here: {flyer_url}"

    for user in users:
        phone = user[0]
        send_sms(phone, message_body)

@app.route('/api/delete_recipient', methods=['POST'])
def delete_recipient():
    data = request.get_json()
    phone = data.get('phone')
    if not phone:
        return jsonify({'error': 'Phone number is required'}), 400

    conn = create_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM signups WHERE phone = %s", (phone,))
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify({'error': 'User not found'}), 404
        return jsonify({'success': True}), 200
    except Exception as e:
        print(f"DB error: {e}")
        return jsonify({'error': 'Database error'}), 500
    finally:
        conn.close()

@app.route('/')
def home():
    return "Backend is live."

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=False, host='0.0.0.0', port=port)
    #app.run(debug=True, port=5001, host="0.0.0.0")
