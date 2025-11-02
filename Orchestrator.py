from flask import Flask, request, jsonify, render_template
import uuid
import threading
import boto3
import os
import time
import sys
import json

app = Flask(__name__)

# Global log storage for the web console
app_logs = []

def log_message(message):
    """Append a timestamped log message to the global app_logs list."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    full_message = f"[{timestamp}] {message}"
    app_logs.append(full_message)

# Configuration for S3 tasks and processing.
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '10'))
ACK_TIMEOUT = int(os.environ.get('ACK_TIMEOUT', '60'))  # seconds to wait for ack
PROCESSING_TIMEOUT = int(os.environ.get('PROCESSING_TIMEOUT', '600'))  # seconds allowed for processing

# Email configuration for SES (for shutdown notification)
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'orchestrator@default.org')
RECIPIENT_EMAIL = os.environ.get('RECIPIENT_EMAIL', 'name@default.ai')
AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')

# Parameter Store key for the orchestrator URL.
SSM_PARAM_NAME = '/orchestrator/url'

# S3 bucket & key for configuration file
CONFIG_BUCKET = "bucket_default.main"
CONFIG_KEY = "configuration/config.json"

def load_config():
    """Loads the configuration from S3 and returns it as a dict."""
    s3 = boto3.client('s3', region_name=AWS_REGION)
    try:
        response = s3.get_object(Bucket=CONFIG_BUCKET, Key=CONFIG_KEY)
        config_str = response['Body'].read().decode('utf-8')
        c_config = json.loads(config_str)
        log_message("Loaded config: " + str(c_config))
        return c_config
    except Exception as e:
        log_message("Error loading config from S3: " + str(e))
        print("Error loading config from S3:", e)
        sys.exit(1)


def parse_s3_path(s3_path):
    if s3_path.startswith("s3://"):
        s3_path = s3_path[len("s3://"):]
    parts = s3_path.split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    return bucket, prefix


# Load configuration and set S3 bucket names from config file.
config = load_config()
INPUT_BUCKET = config.get("input_bucket")
OUTPUT_BUCKET = config.get("output_bucket")
if not INPUT_BUCKET or not OUTPUT_BUCKET:
    log_message("Input or output bucket not found in configuration.")
    print("Input or output bucket not found in configuration.")
    sys.exit(1)

# For our purposes, we use a constant output prefix from our earlier configuration.
OUTPUT_PREFIX = config.get("output_prefix", "processed")

# Global in-memory stores.
workers = {}  # Mapping of worker_id -> worker info.
lock = threading.Lock()


def list_processed_file_numbers():
    """
    List the numeric prefixes of processed files in the output bucket.
    Assumes processed files follow the pattern: NUM.seg_classes.ODS5.png
    """
    s3 = boto3.client('s3', region_name=AWS_REGION)
    processed_numbers = set()
    paginator = s3.get_paginator('list_objects_v2')
    # Optionally, if your processed files are under a specific prefix, use that:
    response_iterator = paginator.paginate(Bucket=OUTPUT_BUCKET, Prefix=OUTPUT_PREFIX)
    for page in response_iterator:
        if "Contents" in page:
            for obj in page["Contents"]:
                key = obj["Key"]
                # Extract numeric prefix before the first dot.
                # For example, "00009.seg_classes.ODS5.png" -> "00009"
                base_name = key.split('/')[-1]
                if base_name:
                    num_prefix = base_name.split('.')[0]
                    processed_numbers.add(num_prefix)
                    processed_numbers.add(num_prefix)
    return processed_numbers


def scan_s3_for_tasks():
    """Scan the input S3 bucket and prefix to list available files."""
    s3 = boto3.client('s3')
    paginator = s3.get_paginator('list_objects_v2')
    input_prefix = config.get("input_prefix", "")
    input_tasks = []
    for page in paginator.paginate(Bucket=INPUT_BUCKET, Prefix=input_prefix):
        if "Contents" in page:
            for obj in page["Contents"]:
                key = obj["Key"]
                # Ignore keys that represent directories (ending with a slash)
                if key.endswith('/') or '.' not in key:
                    continue
                input_tasks.append(key)
    log_message(f"Found {len(input_tasks)} files in input bucket {INPUT_BUCKET} with prefix '{input_prefix}'")
    # Remove existing outputs
    processed_numbers = list_processed_file_numbers()
    log_message(f"Found {len(processed_numbers)} processed files in output bucket {OUTPUT_BUCKET}")
    # Filter out input files that have already been processed.
    filtered_tasks = []
    for task in input_tasks:
        # Extract the filename (ignore the prefix folders)
        base_name = task.split('/')[-1]
        # Extract numeric part from the filename (assuming pattern NUM.jpg)
        numeric_part = base_name.split('.')[0]
        if numeric_part not in processed_numbers:
            filtered_tasks.append(task)
        else:
            log_message(f"Skipping already processed file: {task}")
    return filtered_tasks


# Populate the tasks list at startup.
tasks = scan_s3_for_tasks()
INPUT_PREFIX = config.get('input_prefix', '')
log_message(f"Discovered {len(tasks)} file(s) in {INPUT_BUCKET} with prefix '{INPUT_PREFIX}'")


def all_tasks_completed():
    """Returns True if there are no tasks left in the pool."""
    return len(tasks) == 0


@app.route('/')
def dashboard():
    """Dashboard to view registered worker agents and their current assignments."""
    with lock:
        workers_snapshot = workers.copy()
        for worker_id, info in workers.items():
            # Set default values.
            elapsed = "N/A"
            time_to_timeout = "N/A"
            if info.get('status') == 'processing' and info.get('processing_start'):
                now = time.time()
                elapsed_seconds = now - info['processing_start']
                remaining_seconds = max(PROCESSING_TIMEOUT - elapsed_seconds, 0)
                elapsed = f"{elapsed_seconds:.1f} sec"
                time_to_timeout = f"{remaining_seconds:.1f} sec"
            worker_info = info.copy()
            worker_info['elapsed_time'] = elapsed
            worker_info['time_to_timeout'] = time_to_timeout
            # Safely get assignment (default to an empty dict if None)
            assignment = worker_info.get('assignment') or {}
            if 'file_names' in assignment and isinstance(assignment['file_names'], list):
                stripped_files = [os.path.basename(p) for p in assignment['file_names']]
                assignment['stripped_file_names'] = stripped_files
            worker_info['assignment'] = assignment
            workers_snapshot[worker_id] = worker_info
        logs_snapshot = app_logs.copy()
    return render_template('dashboard.html', workers=workers_snapshot,
                           remaining_tasks=len(tasks), logs=logs_snapshot)


@app.route('/logs')
def get_logs():
    """Endpoint to return the log messages in JSON format."""
    with lock:
        return jsonify({"logs": app_logs})


@app.route('/register', methods=['POST'])
def register_worker():
    """Endpoint for worker agents to register. Worker is marked as 'idle' after registration."""
    worker_data = request.json
    worker_id = str(uuid.uuid4())
    with lock:
        workers[worker_id] = {
            'status': 'idle',
            'details': worker_data,
            'assignment': None,
            'assignment_timestamp': None,
            'processing_start': None,
            'history': [{'status': 'registered', 'details': worker_data, 'timestamp': time.time()}]
        }
    return jsonify({'worker_id': worker_id, 'message': 'Worker registered successfully'}), 200


@app.route('/assignment/<worker_id>', methods=['GET'])
def get_assignment(worker_id):
    """
    Endpoint for workers to poll for an assignment.
    If no tasks remain, a shutdown instruction is sent.
    Otherwise, a batch is assigned and includes:
      - file_names: list of files to process
      - input_bucket: the input S3 bucket name
      - output_bucket and output_prefix for the results.
    """
    with lock:
        worker = workers.get(worker_id)
        if not worker:
            return jsonify({'error': 'Worker not registered'}), 404

        # If the worker already has an active assignment, return it.
        if worker.get('assignment'):
            return jsonify({'assignment': worker['assignment']}), 200

        # If all tasks are complete, issue shutdown.
        if all_tasks_completed():
            shutdown_assignment = {
                'shutdown': True,
                'message': 'All tasks completed. Please shutdown.'
            }
            worker['assignment'] = shutdown_assignment
            worker['status'] = 'shutdown'
            worker['history'].append({'status': 'shutdown', 'details': shutdown_assignment, 'timestamp': time.time()})
            return jsonify({'assignment': shutdown_assignment}), 200

        # Otherwise, assign a new batch.
        if tasks:
            batch = tasks[:BATCH_SIZE]
            del tasks[:BATCH_SIZE]  # Remove batch from pool.
            assignment = {
                'file_names': batch,
                'input_bucket': INPUT_BUCKET,
                'input_prefix': INPUT_PREFIX,
                'output_bucket': OUTPUT_BUCKET,
                'output_prefix': OUTPUT_PREFIX
            }
        else:
            assignment = {
                'file_names': [],
                'input_bucket': INPUT_BUCKET,
                'output_bucket': OUTPUT_BUCKET,
                'output_prefix': OUTPUT_PREFIX,
                'message': 'No tasks remaining'
            }
        worker['assignment'] = assignment
        worker['assignment_timestamp'] = time.time()
        worker['status'] = 'waiting_ack'
        worker['history'].append({'status': 'assignment_assigned', 'details': assignment, 'timestamp': time.time()})
    return jsonify({'assignment': assignment}), 200


@app.route('/ack/<worker_id>', methods=['POST'])
def acknowledge_assignment(worker_id):
    """Endpoint for workers to acknowledge receipt of an assignment."""
    with lock:
        worker = workers.get(worker_id)
        if not worker:
            return jsonify({'error': 'Worker not registered'}), 404
        if not worker.get('assignment'):
            return jsonify({'error': 'No assignment pending'}), 400
        worker['status'] = 'processing'
        worker['processing_start'] = time.time()
        worker['history'].append({'status': 'processing', 'details': {}, 'timestamp': time.time()})
        worker['assignment_timestamp'] = None
    return jsonify({'message': 'Assignment acknowledged and processing started'}), 200


@app.route('/status/<worker_id>', methods=['POST'])
def update_status(worker_id):
    """
    Endpoint for workers to update their status.
    Special statuses:
      - "completed": task finished, assignment cleared, status reset to "idle"
      - "failed": task failed, assignment returned to pool, status reset to "idle"
      - "shutting-down": worker is shutting down.
    """
    status_update = request.json.get('status', '').lower()
    details = request.json.get('details', {})

    with lock:
        worker = workers.get(worker_id)
        if not worker:
            return jsonify({'error': 'Worker not registered'}), 404

        if worker.get('assignment'):
            current_assignment = worker['assignment']
            if status_update == 'completed':
                worker['history'].append({'status': 'completed', 'details': details, 'timestamp': time.time()})
                worker['assignment'] = None
                worker['assignment_timestamp'] = None
                worker['processing_start'] = None
                worker['status'] = 'idle'
            elif status_update == 'failed':
                tasks.extend(current_assignment.get('file_names', []))
                worker['history'].append({'status': 'failed', 'details': details, 'timestamp': time.time()})
                worker['assignment'] = None
                worker['assignment_timestamp'] = None
                worker['processing_start'] = None
                worker['status'] = 'failed'
                log_message(f"Assignment failed to process on {worker_id}, task returned to pool")
            elif status_update == 'processing':
                worker['history'].append({'status': 'processing', 'details': details, 'timestamp': time.time()})
                worker['status'] = 'processing'
            elif status_update == 'shutting-down':
                worker['history'].append({'status': 'shutting-down', 'details': details, 'timestamp': time.time()})
                worker['assignment'] = None
                worker['assignment_timestamp'] = None
                worker['processing_start'] = None
                worker['status'] = 'shutting-down'
            else:
                worker['history'].append({'status': status_update, 'details': details, 'timestamp': time.time()})
                worker['status'] = status_update
        else:
            if status_update == 'shutting-down':
                worker['history'].append({'status': 'shutting-down', 'details': details, 'timestamp': time.time()})
                worker['status'] = 'shutting-down'
            else:
                worker['history'].append({'status': status_update, 'details': details, 'timestamp': time.time()})
                worker['status'] = 'unavailable'
                log_message(f"Failed to access worker {worker_id}")

    return jsonify({'message': 'Status updated successfully'}), 200


def send_email_notification():
    """Sends an email via AWS SES notifying that processing is complete."""
    ses = boto3.client('ses', region_name=AWS_REGION)
    subject = "Processing Completed"
    body_text = "All worker agents are in shutting-down mode.\r\nProcessing is complete."
    try:
        response = ses.send_email(
            Source=SENDER_EMAIL,
            Destination={'ToAddresses': [RECIPIENT_EMAIL]},
            Message={
                'Subject': {'Data': subject},
                'Body': {'Text': {'Data': body_text}}
            }
        )
        print("Email notification sent. Message ID:", response.get('MessageId'))
    except Exception as e:
        print("Error sending email:", e)


def shutdown_instance():
    """Shuts down the EC2 instance."""
    print("Shutting down the EC2 instance now...")
    os.system("sudo shutdown now")
    sys.exit(0)


def shutdown_notifier():
    """
    Background thread that checks if all workers are in shutting-down mode.
    If yes, it sends an email notification and shuts down the instance.
    """
    while True:
        time.sleep(10)
        with lock:
            if workers and all(worker.get('status') == 'shutting-down' for worker in workers.values()):
                print("All workers are shutting down.")
                send_email_notification()
                shutdown_instance()
                break


def timeout_checker():
    """
    Background thread that checks for workers that time out during ack or processing.
    """
    while True:
        time.sleep(5)
        now = time.time()
        with lock:
            for worker_id, worker in list(workers.items()):
                if worker.get('status') == 'waiting_ack' and worker.get('assignment_timestamp'):
                    elapsed_ack = now - worker['assignment_timestamp']
                    if elapsed_ack > ACK_TIMEOUT:
                        assignment = worker.get('assignment', {})
                        file_batch = assignment.get('file_names', [])
                        tasks.extend(file_batch)
                        worker['history'].append(
                            {'status': 'ack_timeout', 'details': {'elapsed': elapsed_ack}, 'timestamp': now})
                        worker['status'] = 'unavailable'
                        worker['assignment'] = None
                        worker['assignment_timestamp'] = None
                        log_message(f"Worker {worker_id} timed out waiting for ack. Returned {len(file_batch)} files to pool.")
                if worker.get('status') == 'processing' and worker.get('processing_start'):
                    elapsed_processing = now - worker['processing_start']
                    if elapsed_processing > PROCESSING_TIMEOUT:
                        assignment = worker.get('assignment', {})
                        file_batch = assignment.get('file_names', [])
                        tasks.extend(file_batch)
                        worker['history'].append(
                            {'status': 'processing_timeout', 'details': {'elapsed': elapsed_processing},
                             'timestamp': now})
                        worker['status'] = 'unavailable'
                        worker['assignment'] = None
                        worker['processing_start'] = None
                        log_message(f"Worker {worker_id} timed out during processing. Returned {len(file_batch)} files to pool.")


checker_thread = threading.Thread(target=timeout_checker, daemon=True)
checker_thread.start()

shutdown_thread = threading.Thread(target=shutdown_notifier, daemon=True)
shutdown_thread.start()


def publish_orchestrator_url(orchestrator_url):
    """
    Publishes the orchestrator's URL to AWS Systems Manager Parameter Store.
    """
    ssm = boto3.client('ssm', region_name=AWS_REGION)
    try:
        ssm.put_parameter(
            Name=SSM_PARAM_NAME,
            Value=orchestrator_url,
            Type='String',
            Overwrite=True
        )
        print(f"Published orchestrator URL '{orchestrator_url}' to Parameter Store under {SSM_PARAM_NAME}.")
    except Exception as e:
        print("Error publishing orchestrator URL:", e)


if __name__ == '__main__':
    # Suppose the orchestrator's public URL is known.
    print("Publishing orchestrator URL to Parameter Store...")
    orchestrator_url_add = "http://orchestrator.default.org:5000"
    publish_orchestrator_url(orchestrator_url_add)
    print("Starting Orchestrator.py")
    app.run(host='0.0.0.0', port=5000, debug=True)
