import requests
import time
import os
import sys
import subprocess
import shutil
import boto3
import signal
import threading


import ShellRunner

# AWS region and Parameter Store configuration.
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SSM_PARAM_NAME = "/orchestrator/url"


def get_orchestrator_url():
    """
    Retrieves the orchestrator URL from AWS Systems Manager Parameter Store.
    """
    ssm = boto3.client('ssm', region_name=AWS_REGION)
    try:
        response = ssm.get_parameter(Name=SSM_PARAM_NAME)
        orchestrator_url = response['Parameter']['Value']
        print(f"Retrieved orchestrator URL: {orchestrator_url}")
        return orchestrator_url
    except Exception as e:
        print("Error retrieving orchestrator URL from Parameter Store:", e)
        return None


ORCHESTRATOR_URL = get_orchestrator_url()
if ORCHESTRATOR_URL is None:
    print("Unable to retrieve orchestrator URL. Exiting.")
    sys.exit(1)

POLL_INTERVAL = 10  # seconds between polling for an assignment

# Local directories for processing.
LOCAL_INPUT_DIR = "/tmp/input_files"
LOCAL_OUTPUT_DIR = "/tmp/output_files"

os.makedirs(LOCAL_INPUT_DIR, exist_ok=True)
os.makedirs(LOCAL_OUTPUT_DIR, exist_ok=True)

s3_client = boto3.client("s3")

g_worker_id = None
# Global flag to prevent multiple shutdowns.
shutdown_in_progress = False
shutdown_lock = threading.Lock()

def start_docker_container():
    """
    Pulls the Docker image 'IM' from Docker Hub and runs a container named 'rgo_container'
    mapping LOCAL_INPUT_DIR to /app/Inputs and LOCAL_OUTPUT_DIR to /app/Outputs.
    """

    folder_map = {'/app/Inputs': LOCAL_INPUT_DIR, '/app/Outputs': LOCAL_OUTPUT_DIR}
    status = ShellRunner.docker_pull_and_mount(folder_map)
    return status


def has_gpu() -> bool:
    """
    Returns True if at least one NVIDIA GPU is available on the system,
    otherwise returns False.
    """
    try:
        # nvidia-smi -L lists each GPU with an ID and name
        output = subprocess.check_output(["nvidia-smi", "-L"])
        # If we get any non-empty output, it means at least one GPU is present
        return bool(output.strip())
    except (FileNotFoundError, subprocess.CalledProcessError):
        # FileNotFoundError -> nvidia-smi is not installed
        # CalledProcessError -> nvidia-smi command error
        return False


def register_worker():
    """Register this worker with the orchestrator and return the worker_id."""
    global  g_worker_id
    gpu_status = 'CPU'
    if has_gpu():
        gpu_status = 'GPU'

    # Get a token
    token = requests.put(
        "http://169.254.169.254/latest/api/token",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        timeout=2
    ).text
    # Use it to get instance type
    instance_type = requests.get(
        "http://169.254.169.254/latest/meta-data/instance-type",
        headers={"X-aws-ec2-metadata-token": token},
        timeout=2
    ).text

    payload = {
        "hostname": os.uname().nodename,
        "capabilities": ["processing", instance_type, gpu_status]
    }
    try:
        response = requests.post(f"{ORCHESTRATOR_URL}/register", json=payload)
        data = response.json()
        g_worker_id = data.get("worker_id")
        print(f"Registered successfully with worker_id: {g_worker_id}")
        return g_worker_id
    except Exception as e:
        print("Error registering worker:", e)
        sys.exit(1)


def poll_for_assignment(worker_id):
    """Poll the orchestrator for an assignment."""
    try:
        response = requests.get(f"{ORCHESTRATOR_URL}/assignment/{worker_id}")
        assignment = response.json().get("assignment")
        return assignment
    except Exception as e:
        print("Error polling for assignment:", e)
        return None


def send_ack(worker_id):
    """Send an acknowledgement for the received assignment."""
    try:
        response = requests.post(f"{ORCHESTRATOR_URL}/ack/{worker_id}")
        print("Ack sent. Response:", response.json())
    except Exception as e:
        print("Error sending ack:", e)


def update_status(worker_id, status, details=None):
    """Update the worker's status at the orchestrator."""
    if details is None:
        details = {}
    payload = {"status": status, "details": details}
    try:
        response = requests.post(f"{ORCHESTRATOR_URL}/status/{worker_id}", json=payload)
        print(f"Status '{status}' updated. Response:", response.json())
    except Exception as e:
        print(f"Error updating status to '{status}':", e)


def download_files(file_names, input_bucket):
    """
    Download each file from the provided input_bucket into LOCAL_INPUT_DIR.
    Returns (True, "") if successful; otherwise (False, error_message).
    """
    for key in file_names:
        local_path = os.path.join(LOCAL_INPUT_DIR, os.path.basename(key))
        try:
            print(f"Downloading s3://{input_bucket}/{key} to {local_path}")
            s3_client.download_file(input_bucket, key, local_path)
        except Exception as e:
            error_msg = f"Error downloading {key} from bucket {input_bucket}: {e}"
            print(error_msg)
            return False, error_msg
    return True, ""


def run_segmentation():
    """
    Run the segmentation script using the command:
      run_segmentation.sh <LocalInputPath> <LocalOutputPath>
    """
    try:
        ShellRunner.exec_in_container('rgo_container', 'run_segmentation.sh',
                                      ['/app/Inputs', '/app/Outputs'])
        return True, ""
    except subprocess.CalledProcessError as e:
        error_msg = f"Error running segmentation script: {e.stderr}"
        print(error_msg)
        return False, error_msg


def upload_results(output_bucket, output_prefix):
    """
    Upload all files from LOCAL_OUTPUT_DIR to the specified output S3 bucket under output_prefix.
    """
    for root, _, files in os.walk(LOCAL_OUTPUT_DIR):
        for file in files:
            local_file = os.path.join(root, file)
            s3_key = os.path.join(output_prefix, file)
            try:
                print(f"Uploading {local_file} to s3://{output_bucket}/{s3_key}")
                s3_client.upload_file(local_file, output_bucket, s3_key)
            except Exception as e:
                error_msg = f"Error uploading {local_file} to bucket {output_bucket}: {e}"
                print(error_msg)
                return False, error_msg
    return True, ""


def clear_local_directories():
    """Clear the contents of the local input and output directories."""
    for directory in [LOCAL_INPUT_DIR, LOCAL_OUTPUT_DIR]:
        # List all items in the directory
        for filename in os.listdir(directory):
            file_path = os.path.join(directory, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                    print(f"Removed file: {file_path}")
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
                    print(f"Removed directory: {file_path}")
            except Exception as e:
                print(f"Failed to delete {file_path}. Reason: {e}")


def process_files(file_names, input_bucket, output_bucket, output_prefix):
    """
    Full processing workflow:
      1. Download files from input_bucket.
      2. Run segmentation script.
      3. Upload output to output S3 bucket.
      4. Clear local directories.
    Returns (True, "") if successful; otherwise (False, error_message).
    """
    print("Starting processing for files:", file_names)

    success, message = download_files(file_names, input_bucket)
    if not success:
        return False, message

    success, message = run_segmentation()
    if not success:
        return False, message

    success, message = upload_results(output_bucket, output_prefix)
    if not success:
        return False, message

    clear_local_directories()
    print("Processing completed successfully for batch.")
    return True, ""


def remove_local_directories():
    """
    Removes the local input and output directories entirely.
    """
    for directory in [LOCAL_INPUT_DIR, LOCAL_OUTPUT_DIR]:
        try:
            shutil.rmtree(directory)
            print(f"Removed directory: {directory}")
        except Exception as e:
            print(f"Error removing directory {directory}: {e}")


def shutdown_instance(signum=0):
    """Shutdown the EC2 instance."""
    global shutdown_in_progress
    with shutdown_lock:
        if shutdown_in_progress:
            return
        shutdown_in_progress = True

    print("Initiating shutdown sequence...")
    if not ShellRunner.stop_docker_container():
        print('Failed to stop container')
    remove_local_directories()

    print("Terminating EC2 instance...")
    # Report shutdown to the orchestrator before exiting.
    try:
        update_status(g_worker_id, "shutting-down", {"reason": f"Received signal {signum}"})
    except Exception as e:
        print("Error updating status on shutdown:", e)
    time.sleep(5)
    os.system("sudo shutdown now")
    sys.exit(0)


# Define a handler for termination signals.
def handle_shutdown_signal(signum, frame):
    shutdown_instance(signum)

# Register signal handlers.
signal.signal(signal.SIGTERM, handle_shutdown_signal)
signal.signal(signal.SIGINT, handle_shutdown_signal)


def main():
    # Register worker first.
    worker_id = register_worker()

    # Attempt to start the Docker container.
    if not start_docker_container():
        error_detail = "Failed to start Docker container 'rgo_container'."
        update_status(worker_id, "failed", {"error": error_detail})
        shutdown_instance()

    try:
        while True:
            print("Polling for assignment...")
            assignment = poll_for_assignment(worker_id)
            if not assignment:
                print("No assignment received. Waiting...")
                time.sleep(POLL_INTERVAL)
                continue

            if assignment.get("shutdown"):
                print("Shutdown command received from orchestrator:", assignment.get("message"))
                update_status(worker_id, "shutting-down", {"reason": "Received shutdown command"})
                shutdown_instance()

            file_names = assignment.get("file_names", [])
            input_bucket = assignment.get("input_bucket")  # Retrieved from task message.
            output_bucket = assignment.get("output_bucket")
            output_prefix = assignment.get("output_prefix")

            if file_names and input_bucket:
                send_ack(worker_id)
                update_status(worker_id, "processing", {"files": file_names})
                try:
                    success, error_detail = process_files(file_names, input_bucket, output_bucket, output_prefix)
                    if success:
                        update_status(worker_id, "completed", {"processed_files": file_names})
                    else:
                        update_status(worker_id, "failed", {"files": file_names, "error": error_detail})
                except Exception as e:
                    update_status(worker_id, "failed", {"files": file_names, "error": str(e)})
                    shutdown_instance()
            else:
                print("No files assigned or missing input bucket. Waiting...")
            time.sleep(POLL_INTERVAL)
    except Exception as ex:
        update_status(worker_id, "failed", {"error": f"Unhandled exception in main loop: {ex}"})
        shutdown_instance()


if __name__ == '__main__':
    main()
