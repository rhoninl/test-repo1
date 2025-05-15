import os
import asyncio
import json
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

import rclpy
from rclpy.node import Node
from std_msgs.msg import Bool
from sensor_msgs.msg import JointState
from fairino_msgs.srv import SetEnable, JointMove

# Environment variables for configuration
ROS_DOMAIN_ID = int(os.getenv("ROS_DOMAIN_ID", "0"))
ROS2_NODE_NAME = os.getenv("ROS2_NODE_NAME", "fairino_http_bridge")
ROS2_NAMESPACE = os.getenv("ROS2_NAMESPACE", "")
DEVICE_IP = os.getenv("DEVICE_IP", "localhost")  # Not strictly used for ROS2, but kept for compatibility
HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))

# ROS2 client node in asyncio context
class FairinoBridgeNode(Node):
    def __init__(self, node_name: str, namespace: str):
        super().__init__(node_name, namespace=namespace)
        self.state = {
            "joint_names": [],
            "positions": [],
            "velocities": [],
            "efforts": [],
            "timestamp": None
        }
        self.status = {}
        self._joint_state_event = asyncio.Event()
        self._status_event = asyncio.Event()

        self.create_subscription(JointState, "/joint_states", self.joint_state_callback, 10)

        # Service clients
        self.enable_cli = self.create_client(SetEnable, "/robot/set_enable")
        self.move_cli = self.create_client(JointMove, "/robot/joint_move")

    def joint_state_callback(self, msg: JointState):
        self.state = {
            "joint_names": list(msg.name),
            "positions": list(msg.position),
            "velocities": list(msg.velocity),
            "efforts": list(msg.effort),
            "timestamp": msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
        }
        self._joint_state_event.set()

    async def wait_for_joint_states(self, timeout=2.0):
        try:
            await asyncio.wait_for(self._joint_state_event.wait(), timeout)
            self._joint_state_event.clear()
            return self.state
        except asyncio.TimeoutError:
            raise TimeoutError("Timeout waiting for joint states")

    async def set_power(self, enable: bool):
        await self.enable_cli.wait_for_service(timeout_sec=2.0)
        req = SetEnable.Request()
        req.enable = enable
        future = self.enable_cli.call_async(req)
        rclpy.spin_until_future_complete(self, future)
        if future.result() is not None and future.result().success:
            return {"result": "ok"}
        else:
            msg = getattr(future.result(), "message", "Unknown error")
            raise RuntimeError(f"Failed to set power: {msg}")

    async def move(self, move_data: Dict[str, Any]):
        await self.move_cli.wait_for_service(timeout_sec=2.0)
        req = JointMove.Request()
        # move_data must contain 'positions', optionally 'velocities', 'accelerations', 'duration'
        req.positions = move_data.get("positions", [])
        req.velocities = move_data.get("velocities", [])
        req.accelerations = move_data.get("accelerations", [])
        req.duration = float(move_data.get("duration", 0.0))
        future = self.move_cli.call_async(req)
        rclpy.spin_until_future_complete(self, future)
        if future.result() is not None and future.result().success:
            return {"result": "ok"}
        else:
            msg = getattr(future.result(), "message", "Unknown error")
            raise RuntimeError(f"Failed to execute move: {msg}")

# Singleton ROS2 node for FastAPI
class ROS2Manager:
    _node = None

    @classmethod
    def get_node(cls):
        if cls._node is None:
            rclpy.init(args=None, domain_id=ROS_DOMAIN_ID)
            cls._node = FairinoBridgeNode(
                ROS2_NODE_NAME,
                ROS2_NAMESPACE
            )
        return cls._node

    @classmethod
    def shutdown(cls):
        if cls._node is not None:
            cls._node.destroy_node()
            rclpy.shutdown()
            cls._node = None

app = FastAPI()

@app.get("/state")
async def get_state():
    node = ROS2Manager.get_node()
    try:
        state = await node.wait_for_joint_states(timeout=2.0)
        return JSONResponse(content={"status": "ok", "state": state})
    except Exception as e:
        raise HTTPException(status_code=504, detail=str(e))

@app.post("/power")
async def set_power(request: Request):
    node = ROS2Manager.get_node()
    try:
        data = await request.json()
        enable = data.get("enable")
        if enable is None or not isinstance(enable, bool):
            raise HTTPException(status_code=400, detail="Payload must contain boolean 'enable' field.")
        result = await node.set_power(enable)
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/move")
async def move(request: Request):
    node = ROS2Manager.get_node()
    try:
        move_data = await request.json()
        if not isinstance(move_data, dict):
            raise HTTPException(status_code=400, detail="Payload must be a valid JSON object.")
        positions = move_data.get("positions")
        if positions is None or not isinstance(positions, list):
            raise HTTPException(status_code=400, detail="Payload must include 'positions' list.")
        result = await node.move(move_data)
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.on_event("shutdown")
def on_shutdown():
    ROS2Manager.shutdown()

if __name__ == "__main__":
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT)