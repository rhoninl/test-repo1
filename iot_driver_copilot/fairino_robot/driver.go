package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/joho/godotenv"
	"github.com/rs/cors"
	"golang.org/x/sync/semaphore"
	"gopkg.in/yaml.v3"

	// ROS 2 Go Client
	rclgo "github.com/sequenceplanner/rclgo"
	"github.com/sequenceplanner/rclgo/pkg/rclgo/ros2/types/std_msgs"
)

type StatusResponse struct {
	JointPositions      map[string]float64 `json:"joint_positions"`
	JointFeedback       map[string]float64 `json:"joint_feedback"`
	RobotPoses          map[string]float64 `json:"robot_poses"`
	EndEffectorState    map[string]float64 `json:"end_effector_state"`
	PlanningGroups      []string           `json:"planning_groups"`
	RealTimeFeedback    map[string]float64 `json:"real_time_feedback"`
	Synchronization     string             `json:"synchronization_status"`
}

type CommandRequest struct {
	Command  string                 `json:"command" yaml:"command"`
	Params   map[string]interface{} `json:"params" yaml:"params"`
}

type CommandResponse struct {
	Status  string      `json:"status"`
	Message string      `json:"message,omitempty"`
	Data    interface{} `json:"data,omitempty"`
}

var (
	deviceIP       string
	ros2DomainID   int
	serverHost     string
	serverPort     string
	ros2NodeName   string
	ros2StatusTopic string
	ros2CommandTopic string
	readTimeout    time.Duration
	writeTimeout   time.Duration
	sem            = semaphore.NewWeighted(1)
)

func getEnv(key, fallback string) string {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	return v
}

func parseEnv() error {
	deviceIP = getEnv("FAIRINO_ROBOT_IP", "127.0.0.1")
	serverHost = getEnv("HTTP_SERVER_HOST", "0.0.0.0")
	serverPort = getEnv("HTTP_SERVER_PORT", "8080")
	ros2DomainID, _ = strconv.Atoi(getEnv("ROS2_DOMAIN_ID", "0"))
	ros2NodeName = getEnv("ROS2_NODE_NAME", "fairino_http_bridge")
	ros2StatusTopic = getEnv("ROS2_STATUS_TOPIC", "/robot/status")
	ros2CommandTopic = getEnv("ROS2_COMMAND_TOPIC", "/robot/command")
	readTimeoutSec, _ := strconv.Atoi(getEnv("HTTP_READ_TIMEOUT", "10"))
	writeTimeoutSec, _ := strconv.Atoi(getEnv("HTTP_WRITE_TIMEOUT", "10"))
	readTimeout = time.Duration(readTimeoutSec) * time.Second
	writeTimeout = time.Duration(writeTimeoutSec) * time.Second
	return nil
}

func main() {
	_ = godotenv.Load() // Optional: load .env if present
	if err := parseEnv(); err != nil {
		panic(err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/status", handleStatus)
	mux.HandleFunc("/cmd", handleCommand)

	handler := cors.AllowAll().Handler(mux)

	srv := &http.Server{
		Addr:         serverHost + ":" + serverPort,
		Handler:      handler,
		ReadTimeout:  readTimeout,
		WriteTimeout: writeTimeout,
	}

	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		panic(err)
	}
}

func handleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	// Mutex to prevent concurrent ROS node usage
	if !sem.TryAcquire(1) {
		http.Error(w, "System busy", http.StatusTooManyRequests)
		return
	}
	defer sem.Release(1)

	node, err := rclgo.NewNode(ctx, ros2NodeName+"_status", "", rclgo.WithDomainID(uint(ros2DomainID)))
	if err != nil {
		http.Error(w, "ROS2 node error: "+err.Error(), http.StatusInternalServerError)
		return
	}
	defer node.Close()

	type StatusMsg struct {
		JointPositions      map[string]float64 `json:"joint_positions"`
		JointFeedback       map[string]float64 `json:"joint_feedback"`
		RobotPoses          map[string]float64 `json:"robot_poses"`
		EndEffectorState    map[string]float64 `json:"end_effector_state"`
		PlanningGroups      []string           `json:"planning_groups"`
		RealTimeFeedback    map[string]float64 `json:"real_time_feedback"`
		Synchronization     string             `json:"synchronization_status"`
	}

	var response StatusMsg
	gotMsg := make(chan struct{})
	sub, err := node.NewSubscription(ros2StatusTopic, std_msgs.MsgStringTypeSupport, func(msg *rclgo.Msg) {
		// Assume robot publishes YAML or JSON string in std_msgs/String
		strMsg := msg.Data.(*std_msgs.MsgString)
		if err := yaml.Unmarshal([]byte(strMsg.Data), &response); err != nil {
			_ = json.Unmarshal([]byte(strMsg.Data), &response) // Try JSON
		}
		close(gotMsg)
	})
	if err != nil {
		http.Error(w, "ROS2 subscription error: "+err.Error(), http.StatusInternalServerError)
		return
	}
	defer sub.Close()

	select {
	case <-gotMsg:
	case <-ctx.Done():
		http.Error(w, "Timeout waiting for status", http.StatusGatewayTimeout)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(response)
}

func handleCommand(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}

	var cmdReq CommandRequest
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		http.Error(w, "Invalid body", http.StatusBadRequest)
		return
	}
	contentType := r.Header.Get("Content-Type")
	switch contentType {
	case "application/json":
		if err := json.Unmarshal(body, &cmdReq); err != nil {
			http.Error(w, "Bad JSON: "+err.Error(), http.StatusBadRequest)
			return
		}
	case "application/x-yaml", "text/yaml", "application/yaml":
		if err := yaml.Unmarshal(body, &cmdReq); err != nil {
			http.Error(w, "Bad YAML: "+err.Error(), http.StatusBadRequest)
			return
		}
	default:
		// Try JSON first, then YAML
		if err := json.Unmarshal(body, &cmdReq); err != nil {
			if err := yaml.Unmarshal(body, &cmdReq); err != nil {
				http.Error(w, "Unsupported content type", http.StatusBadRequest)
				return
			}
		}
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	if !sem.TryAcquire(1) {
		http.Error(w, "System busy", http.StatusTooManyRequests)
		return
	}
	defer sem.Release(1)

	node, err := rclgo.NewNode(ctx, ros2NodeName+"_cmd", "", rclgo.WithDomainID(uint(ros2DomainID)))
	if err != nil {
		http.Error(w, "ROS2 node error: "+err.Error(), http.StatusInternalServerError)
		return
	}
	defer node.Close()

	// Compose command message as JSON or YAML string
	var cmdMsgStr string
	if contentType == "application/json" || contentType == "" {
		b, _ := json.Marshal(cmdReq)
		cmdMsgStr = string(b)
	} else {
		b, _ := yaml.Marshal(cmdReq)
		cmdMsgStr = string(b)
	}

	pub, err := node.NewPublisher(ros2CommandTopic, std_msgs.MsgStringTypeSupport)
	if err != nil {
		http.Error(w, "ROS2 publisher error: "+err.Error(), http.StatusInternalServerError)
		return
	}
	defer pub.Close()

	// Publish the command
	msg := std_msgs.NewMsgString()
	msg.Data = cmdMsgStr
	if err := pub.Publish(msg); err != nil {
		http.Error(w, "Failed to publish: "+err.Error(), http.StatusInternalServerError)
		return
	}

	// Optionally, listen for a feedback/ack topic, here we simply acknowledge
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(CommandResponse{
		Status:  "accepted",
		Message: "Command published",
	})
}