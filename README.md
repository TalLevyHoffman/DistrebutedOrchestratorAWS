# Architecture Overview for Distributed S3 Processing with EC2 Spot Instances

This document describes the architecture for a distributed processing system built on AWS. 
The solution uses EC2 Spot instances as worker agents that pull file processing tasks from a central orchestrator. 
The orchestrator distributes batches of files from an S3 input path and collects results via an S3 output bucket. 
A shared configuration using AWS Systems Manager Parameter Store is used to publish the orchestrator's public URL.

## Components

1. **Orchestrator (EC2 Instance Running Flask Application)**
   - **Responsibilities:**
     - Scan an S3 input bucket for files.
     - Partition files into batches (using a configurable batch size).
     - Maintain a registry of worker agents.
     - Distribute assignments to worker agents via REST endpoints.
     - Accept status updates from workers (e.g., ack, processing, completed, failed, shutting-down).
     - Publish its public URL to AWS Systems Manager Parameter Store.
     - Monitor worker timeouts and, when all tasks are completed and all workers are shutting down, notify via email and shut itself down.
   - **Endpoints:**
     - `/register` – Workers register here.
     - `/assignment/<worker_id>` – Workers poll for tasks.
     - `/ack/<worker_id>` – Workers acknowledge assignment receipt.
     - `/status/<worker_id>` – Workers update processing status.
   - **Dashboard:**
     - A simple web UI shows worker registration, status, assignment details, and error logs.

2. **Worker Agents (EC2 Spot Instances)**
   - **Responsibilities:**
     - Retrieve the orchestrator URL from AWS Systems Manager Parameter Store.
     - Register with the orchestrator.
     - Pull a designated Docker image from Docker Hub.
     - Run a container (named **rgo_container**) with volume mappings:
       - Maps a local input directory (e.g., `/tmp/input_files`) to `/app/Inputs`.
       - Maps a local output directory (e.g., `/tmp/output_files`) to `/app/Outputs`.
     - Poll for assignments and, when assigned a batch:
       - Download files from the input S3 bucket (bucket provided in the assignment).
       - Execute the segmentation process by running a shell script (e.g., `run_segmentation.sh`) inside the Docker container.
       - Upload the resulting output files to the output S3 bucket.
       - Clear local files (while preserving the directory structure) after each task.
       - Report status updates (ack, processing, completed, failed, shutting-down) to the orchestrator.
     - When a shutdown command is received or if a critical error occurs (e.g., failing to start the Docker container), the worker:
       - Attempts to gracefully stop and remove the Docker container.
       - Cleans up its local directories.
   
3. **Shared Configuration (AWS Systems Manager Parameter Store)**
   - The orchestrator publishes its public URL to Parameter Store.
   - Workers query Parameter Store at startup to determine the orchestrator URL.

## Communication Flow

1. **Worker Registration:**
   - A worker calls the `/register` endpoint to get a unique `worker_id`.

2. **Assignment:**
   - The worker polls `/assignment/<worker_id>`.
   - The orchestrator assigns a batch of files (with associated S3 details) or a shutdown command when no tasks remain.
   - The assignment payload includes:
     - `file_names`: list of files to process.
     - `input_bucket`: input S3 bucket name.
     - `output_bucket` and `output_prefix`: for uploading results.
   
3. **Processing:**
   - After receiving an assignment, the worker sends an acknowledgment (`/ack/<worker_id>`) and updates its status to "processing."
   - The worker downloads files from the S3 input bucket, runs the segmentation script, uploads results to the S3 output bucket, and clears its local directories.
   - Any errors (such as inaccessible S3 paths) are reported back using the `/status/<worker_id>` endpoint.

4. **Shutdown:**
   - Once all tasks are processed, the orchestrator sends a shutdown assignment.
   - Workers receiving this command update their status to "shutting-down" and proceed to stop and remove the Docker container, clear local directories, and terminate their EC2 instance.
   - When all workers are shutting down, the orchestrator sends a notification email and shuts itself down.

## Fault Tolerance and Timeouts

- **Acknowledgment Timeout:**
  - If a worker fails to acknowledge an assignment within a set time, the assignment is returned to the pool, and the worker is marked unavailable.
- **Processing Timeout:**
  - If a worker takes too long processing a task, it is similarly marked as unavailable and its task returned to the pool.
- **Error Reporting:**
  - Workers report specific errors (e.g., S3 bucket access issues) in status updates, which are logged and displayed in the dashboard.

## Docker Container Setup on Workers

- **Pulling and Running the Container:**
  - At startup, each worker pulls a Docker image from Docker Hub.
  - The worker runs a container named **rgo_container** with volume mappings that link local directories to container directories (`/app/Inputs` and `/app/Outputs`).
- **Container Execution:**
  - The segmentation script (`run_segmentation.sh`) is executed inside the container using `docker exec`.
- **Error Handling:**
  - If the Docker container fails to start, the worker reports this error to the orchestrator and initiates a shutdown of its EC2 instance.
  
## Git Configuration

- **permissions:**
	- This git repository must be permitted for pull using **app password**
  
  
# AWS Configuration Requirements for Distributed Processing System

This document details the AWS configurations needed for deploying the orchestrator and worker agents for the distributed S3 processing solution.

## 1. S3 Buckets

### Input S3 Bucket
- **Purpose:** Stores input files for processing.
- **Configuration:**
  - **Bucket Policy:** Allow read (GetObject) access for the orchestrator and workers.
  - **Folder Structure:** Use a defined prefix (e.g., `/input`) to organize files.
  - **Permissions:** Ensure the IAM roles attached to EC2 instances include `s3:GetObject` permission on this bucket.

### Output S3 Bucket
- **Purpose:** Receives processed output files.
- **Configuration:**
  - **Bucket Policy:** Allow write (PutObject) access for the orchestrator and workers.
  - **Folder Structure:** Use a defined prefix (e.g., `/output`) for uploaded results.
  - **Permissions:** Ensure the IAM roles have `s3:PutObject` permission on this bucket.
  
### Configuration S3 Bucket
- **Purpose:** Holds the processing configuration.
- **Configuration:**
  - **Bucket Policy:** Allow read (GetObject) access for the orchestrator.
  - **Folder Structure:** Use a defined prefix (e.g., `/configuration`) must contain a config.json file with the following fields:
	- input_bucket
	- input_prefix
	- output_bucket
	- output_prefix
  - **Permissions:** Ensure the IAM roles attached to EC2 instances include `s3:GetObject` permission on this bucket.

## 2. EC2 Instances

### Orchestrator Instance
- **Type:** A stable instance (preferably not a Spot instance) that runs the Flask application.
- **Network:**
  - **Public IP or Elastic IP:** Ensure the instance is in a public subnet and has an associated public IP.
  - **Security Group:** Allow inbound traffic on the orchestrator's port (e.g., TCP 5000) from worker IP ranges (or broader if necessary).
- **IAM Role:** Should include:
  - `ssm:PutParameter` to publish the URL to Parameter Store.
  - `s3:ListBucket` and `s3:GetObject` for scanning the input bucket.
  - `s3:PutObject` for uploading to the output bucket.
  - `ses:SendEmail` to send notifications.
  - `ec2:StopInstances` (or permissions to run system shutdown commands if using OS-level shutdown).

### Worker Instances (EC2 Spot Instances)
- **Type:** Spot instances configured to run the worker code.
- **Network:**
  - **Public or Private Subnet:** Must have outbound internet access to reach the orchestrator and AWS Parameter Store.
  - **Security Group:** Allow outbound traffic (typically default settings are sufficient).
- **IAM Role:** Should include:
  - `ssm:GetParameter` to read the orchestrator URL.
  - `s3:GetObject` for downloading from the input bucket.
  - `s3:PutObject` for uploading to the output bucket.
  - Permissions to execute system shutdown (if using OS-level shutdown).

## 3. AWS Systems Manager Parameter Store

- **Parameter:**
  - **Name:** `/orchestrator/url`
  - **Type:** String
  - **Value:** Public URL of the orchestrator (e.g., `http://<orchestrator-public-ip>:5000`).
- **Access:** Ensure the orchestrator’s IAM role can use `ssm:PutParameter` and the workers’ roles can use `ssm:GetParameter`.

## 4. AWS SES (Simple Email Service)

- **Purpose:** To send notifications when all workers are shutting down and processing is complete.
- **Configuration:**
  - **Verified Email Addresses:** Both the sender and recipient emails must be verified (in case of sandbox mode).
  - **IAM Permissions:** The orchestrator’s IAM role must have `ses:SendEmail`.
  - **Region:** Ensure SES is supported in your region or use the correct region.

## 5. Optional: Domain Name and Load Balancer

- **Route 53:** Consider setting up a custom domain/subdomain that points to the orchestrator’s public IP or load balancer.
- **Application Load Balancer (ALB):** 
  - **Usage:** To front the orchestrator for improved availability.
  - **Configuration:** Create an ALB, configure a listener (e.g., port 80/443), and set target group to your orchestrator instance.
  - **DNS:** Use Route 53 to create an alias record pointing to the ALB.

---

## Summary

- **S3 Buckets:** Configure appropriate bucket policies and IAM permissions for file access.
- **EC2 Instances:** Ensure public accessibility for the orchestrator and proper IAM roles for both orchestrator and workers.
- **IAM Roles:** Include necessary S3, SSM, SES, and EC2 shutdown permissions.
- **Parameter Store:** Use Parameter Store to dynamically distribute the orchestrator URL.
- **Optional Load Balancing:** For production environments, consider an ALB with Route 53 for a custom domain and improved resiliency.

These configurations ensure that your distributed processing system operates securely, reliably, and can scale dynamically using EC2 Spot Instances.