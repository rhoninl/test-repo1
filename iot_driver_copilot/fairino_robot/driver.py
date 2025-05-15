import os
import asyncio
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import rclpy
from rclpy.node import Node
from rclpy.executors import SingleThreadedExecutor
from fairino_msgs.msg import JointState, RobotStatus  # Adjust import as per package
from fairino_msgs.srv import SetEnable, JointTrajectory  # Adjust as per actual ROS2 srv/msg definitions

# Read environment variables for configuration
ROS_DOMAIN_ID = os.getenv("ROS_DOMAIN_ID", "0")
DEVICE_IP = os.getenv("FAIRINO_DEVICE_IP", "127.0.0.1")
SERVER_HOST = os.getenv("HTTP_SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("HTTP_SERVER_PORT", "8080"))

os.environ["ROS_DOMAIN_ID"] = str(ROS_DOMAIN_ID)

app = FastAPI()

# Shared state for robot
class RobotState:
    def __init__(self):
        self.joint_state = None
        self.robot_status = None

robot_state = RobotState()

# ROS2 Node running in background
class FairinoROS2Node(Node):
    def __init__(self):
        super().__init__("fairino_http_bridge")
        self.joint_state_sub = self.create_subscription(
            JointState, "/joint_states", self.joint_state_callback, 10
        )
        self.robot_status_sub = self.create_subscription(
            RobotStatus, "/robot_status", self.robot_status_callback, 10
        )
        self.set_enable_cli = self.create_client(SetEnable, "/set_enable")
        self.trajectory_cli = self.create_client(JointTrajectory, "/joint_trajectory")

    def joint_state_callback(self, msg):
        robot_state.joint_state = {
            "name": list(msg.name),
            "position": list(msg.position),
            "velocity": list(msg.velocity),
            "effort": list(msg.effort)
        }

    def robot_status_callback(self, msg):
        robot_state.robot_status = {
            "is_enabled": msg.is_enabled,
            "is_error": msg.is_error,
            "error_code": msg.error_code,
            "mode": msg.mode
        }

# Global ROS2 node and executor
ros2_node = None
executor = None

def spin_ros2():
    global executor
    executor = SingleThreadedExecutor()
    executor.add_node(ros2_node)
    executor.spin()

@app.on_event("startup")
async def startup_event():
    global ros2_node
    rclpy.init(args=None)
    ros2_node = FairinoROS2Node()
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, spin_ros2)

@app.on_event("shutdown")
async def shutdown_event():
    global executor, ros2_node
    if executor:
        executor.shutdown()
    if ros2_node:
        ros2_node.destroy_node()
    rclpy.shutdown()

@app.get("/state")
async def get_state():
    # Compose current state
    if robot_state.joint_state is None or robot_state.robot_status is None:
        raise HTTPException(status_code=503, detail="Robot state unavailable.")
    return JSONResponse({
        "joint_state": robot_state.joint_state,
        "robot_status": robot_state.robot_status
    })

@app.post("/power")
async def set_power(request: Request):
    req_json = await request.json()
    if "enable" not in req_json or not isinstance(req_json["enable"], bool):
        raise HTTPException(status_code=400, detail="Missing or invalid 'enable' field.")
    enable = req_json["enable"]

    # Wait for service
    if not ros2_node.set_enable_cli.wait_for_service(timeout_sec=2.0):
        raise HTTPException(status_code=503, detail="Enable service not available.")

    srv_req = SetEnable.Request()
    srv_req.enable = enable
    future = ros2_node.set_enable_cli.call_async(srv_req)
    while not future.done():
        await asyncio.sleep(0.01)
    result = future.result()
    if result is None:
        raise HTTPException(status_code=500, detail="No response from robot enable service.")
    return JSONResponse({"success": result.success, "message": result.message})

@app.post("/move")
async def move_robot(request: Request):
    req_json = await request.json()
    # Expected: positions, velocities, duration, etc.
    required_fields = ["positions"]
    for field in required_fields:
        if field not in req_json:
            raise HTTPException(status_code=400, detail=f"Missing '{field}' in request.")

    # Wait for service
    if not ros2_node.trajectory_cli.wait_for_service(timeout_sec=2.0):
        raise HTTPException(status_code=503, detail="Joint trajectory service not available.")

    srv_req = JointTrajectory.Request()
    srv_req.positions = req_json.get("positions", [])
    srv_req.velocities = req_json.get("velocities", [])
    srv_req.accelerations = req_json.get("accelerations", [])
    srv_req.time_from_start = req_json.get("time_from_start", 0.0)

    future = ros2_node.trajectory_cli.call_async(srv_req)
    while not future.done():
        await asyncio.sleep(0.01)
    result = future.result()
    if result is None:
        raise HTTPException(status_code=500, detail="No response from joint trajectory service.")
    return JSONResponse({"success": result.success, "message": result.message})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
