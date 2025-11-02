import subprocess
from typing import List

def exec_in_container(container_name: str, script_name: str, params: List[str]):
    try:
        # Set permissions for the script
        cmd = 'docker exec ' + container_name + ' chmod +x /app/' + script_name
        subprocess.run(
            cmd,
            shell=True,
            check=True
        )

        cmd = 'docker exec ' + container_name + ' /app/' + script_name
        for p in params:
            cmd = cmd + ' ' + p
        subprocess.run(
            cmd,
            shell=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"Error running script in container: {e}")
        print(f"Return code: {e.returncode}")
        print(f"Output: {e.output}")

def docker_pull_and_mount(mount_dict: dict, container_name: str = 'rgo_container',
                          image_name: str = 'rgodocker/rgo_seg:latest'):
    try:
        cmd = 'docker pull ' + image_name
        print('Pulling Docker image <' + image_name + '> from Docker Hub...')
        subprocess.run(
            cmd,
            shell=True,
            check=True
        )

        # Check if container with the same name already exists
        cmd = f"docker ps -aqf \"name={container_name}\""
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        container_id = result.stdout.strip()

        if container_id:
            # If it exists, stop and remove it
            print(f"Container '{container_name}' already exists. Stopping and removing...")
            subprocess.run(f"docker stop {container_name}", shell=True, check=True)
            subprocess.run(f"docker rm {container_name}", shell=True, check=True)

        cmd = 'docker run --gpus all -d --name ' + container_name
        for key in mount_dict.keys():
            cmd = cmd + ' -v ' + mount_dict[key] + ':' + key
        cmd = cmd + ' -it ' + image_name

        subprocess.run(
            cmd,
            shell=True,
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error pulling and mounting container: {e}")
        return False

def stop_docker_container(container_name: str = 'rgo_container'):
    """
    Attempts to gracefully stop and remove the Docker container 'rgo_container'.
    """
    try:
        print("Stopping Docker container")
        subprocess.run(["docker", "stop", container_name], check=True)
        print("Removing Docker container")
        subprocess.run(["docker", "rm", container_name], check=True)
        print("Docker container stopped and removed")
        return True
    except subprocess.CalledProcessError as e:
        print("Error stopping/removing Docker container:", e)
        return False

if __name__ == "__main__":
    exec_in_container('rgo_container', 'test_multi_GPU.sh', [])
