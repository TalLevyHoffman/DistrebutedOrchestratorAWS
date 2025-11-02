# AWS Infrastructure Setup for Remote Work System

Below is an overview of the components and steps required for setting up the AWS infrastructure for your Remote Work system.

1. **VPC (Virtual Private Cloud)**
   - Create a VPC (named **RemoteWorkVPC**) that will allow all machines to communicate.

2. **Internet Gateway (IGW)**
   - Create an Internet Gateway (named **RemoteWorkVPC-IGW**) and attach it to the **RemoteWorkVPC**.

3. **Security Group**
   - Create a security group (named **WorkerFlock**) that allows for the outbound and inbound communication necessary for the orchestrator and worker interactions.
   
4. **Route Table**
   - Create a route table with the rule (0.0.0.0/0 -> IGW **RemoteWorkVPC-IGW**) as **OrchestratorRoute**
   
5. **Subnet**
   - Create a subnet **WorkerNet** and assign the route table **OrchestratorRoute** to it associate it with **RemoteWorkVPC**

6. **IAM Role**
   - Create an IAM Role (named **WorkerOrchestrator**) that allocates permissions to different AWS services:
     - Allows S3 bucket read, write, and list operations.
     - Allows access to Parameter Store (SSM) to read global configuration values.
     - Allows EC2 operations such as shutdown.
     - Allows access to external mail services (SES).

7. **KeyPair**
   - Create a KeyPair for secure SSH login (for the Orchestrator).

8. **Launch Template: Worker Machine (Worker)**
   - Create a launch template for a worker machine with the following initialization steps:
     1. Update system packages.
     2. Install Python, Git, and Docker.
     3. Start the Docker service.
     4. Log in to Docker Hub.
     5. Create or navigate to the work directory.
     6. Clone or pull the **AWSTOOLS** repository.
     7. Run `pip install -r requirements.txt` to install all dependencies.
     8. Verify installed versions.
     9. Run `Worker.py`.

9. **Launch Template: Orchestrator Machine (Orchestrator)**
   - Create a launch template for the orchestrator machine with the following steps:
     1. Update system packages.
     2. Install Python and Git.
     3. Create or navigate to the work directory.
     4. Clone or pull the **AWSTOOLS** repository.
     5. Run `pip install -r requirements.txt` to install all dependencies.
     6. Verify installed versions.
     7. Run `Orchestrator.py`.
     8. Use a low-rate, simple CPU machine (e.g., `t2.small`).

10. **Instance**
   - Create an instance based on the Orchestrator launch template (named **Orchestrator**) that is configured to stop while inactive.

11. **Domain**
   - Create a domain in Route 53 (e.g., **default.org**) to provide a fixed public URL.

12. **Elastic IP**
    - Allocate an Elastic IP and associate it with the Orchestrator instance so that it has a consistent external IP address.

13. **Route 53 Record**
    - Create a DNS record in Route 53 that maps the Orchestrator's Elastic IP to a friendly domain name (e.g., **orchestrator.default.org**).

14. **AWS Systems Manager Parameter Store**
    - Create a parameter (e.g., `/orchestrator/url`) that stores the Orchestrator’s public URL.

15. **Amazon WorkMail**
    - In Amazon WorkMail, link the **rgorobotics.org** domain to an email address (e.g., **orchestrator@default.org**) for email notifications and communications.






