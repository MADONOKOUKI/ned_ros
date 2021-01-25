#!/usr/bin/env python

# Lib
import rospy
import actionlib
import threading

import sys

# Commanders
from arm_commander import ArmCommander
from tool_commander import ToolCommander
from tool_commander_simu import ToolCommanderSimu
import moveit_commander
from ArmParametersValidator import ArmParametersValidator
from motor_debug import MotorDebug

from jog_controller import JogController
from niryo_robot_commander.command_enums import *

# Command Status
from niryo_robot_msgs.msg import CommandStatus

# For State Publisher
import tf
from tf import LookupException, ConnectivityException, ExtrapolationException
from tf.transformations import quaternion_from_euler
from math import pi

# Messages
from actionlib_msgs.msg import GoalStatus
from std_msgs.msg import Bool
from geometry_msgs.msg import Quaternion
from niryo_robot_commander.msg import RobotCommand
from niryo_robot_msgs.msg import RobotState, HardwareStatus

# Services
from niryo_robot_msgs.srv import Trigger
from niryo_robot_msgs.srv import SetBool
from niryo_robot_msgs.srv import GetBool

# Action msgs
from niryo_robot_commander.msg import PausePlanExecution
from niryo_robot_commander.msg import RobotMoveAction
from niryo_robot_commander.msg import RobotMoveResult


class RobotCommanderNode:
    """
    This class is in charge of the Robot Commander Node
    It contains:
    - The State Publisher
    - Arm & Tools Commanders

    Its main goal is to interpret command, and launch execution according to this command
    """

    def __init__(self):
        rospy.logdebug("Robot Commander - Entering in Init")
        # Initialize MoveIt!
        moveit_commander.roscpp_initialize(sys.argv)
        # - Load all the sub-commanders
        # First, get Parameters Validator for Arm
        arm_param_validator = ArmParametersValidator(rospy.get_param("/niryo_robot/robot_command_validation"))
        # Initialize Arm
        self.__arm_commander = ArmCommander(arm_param_validator)
        # Initialize tool commander
        if not rospy.get_param("~simu_gripper"):
            self.__tool_commander = ToolCommander()
        else:
            self.__tool_commander = ToolCommanderSimu()

        rospy.logdebug("Robot Commander - Sub Commanders are loaded")

        # Initialize motor debug
        self.__motor_debug = MotorDebug()

        # Dict which link MoveCommand to arm functions
        self.dict_interpreter_move_cmd = {
            # Move to One Pose
            MoveCommandType.JOINTS: self.__arm_commander.set_joint_target,
            MoveCommandType.POSE: self.__arm_commander.set_pose_target_from_cmd,
            MoveCommandType.POSITION: self.__arm_commander.set_position_target,
            MoveCommandType.RPY: self.__arm_commander.set_rpy_target,
            MoveCommandType.POSE_QUAT: self.__arm_commander.set_pose_quat_target,
            MoveCommandType.LINEAR_POSE: self.__arm_commander.set_linear_trajectory,
            MoveCommandType.SHIFT_POSE: self.__arm_commander.set_shift_pose_target,

            # Trajectory
            MoveCommandType.EXECUTE_TRAJ: self.__arm_commander.execute_trajectory,

            # Add-Ons
            MoveCommandType.DRAW_SPIRAL: self.__arm_commander.draw_spiral_trajectory,
        }

        # - Subscribers
        self.__learning_mode_on = True
        rospy.Subscriber('/niryo_robot/learning_mode/state', Bool,
                         self.__callback_learning_mode)

        self.__hardware_status = None
        rospy.Subscriber('/niryo_robot_hardware_interface/hardware_status', HardwareStatus,
                         self.__callback_hardware_status)

        self.__pause_state = PausePlanExecution.STANDBY
        rospy.Subscriber('/niryo_robot_rpi/pause_state', PausePlanExecution,
                         self.__callback_pause_movement)

        # Event which allows to timeout if pause take too long
        self.__pause_finished_event = threading.Event()
        self.__pause_finished_event.set()
        self.__pause_timeout = rospy.get_param("~pause_timeout")

        # - Services
        rospy.Service('~stop_command', Trigger,
                      self.__callback_stop_command)

        rospy.Service('~is_active', GetBool,
                      self.__callback_is_active)

        # Robot Action Server
        self.__current_goal_handle = actionlib.ServerGoalHandle()
        self.__action_server = actionlib.ActionServer('~robot_action', RobotMoveAction,
                                                      goal_cb=self.__callback_goal, cancel_cb=self.__callback_cancel,
                                                      auto_start=False)
        self.__action_server_thread = threading.Thread()
        self.__action_server_lock = threading.Lock()
        self.__command_still_active_max_tries = rospy.get_param("~command_still_active_max_tries")

        # Starting Action server
        self.__start_action_server()

        rospy.logdebug("Robot Commander - Services & Actions server are created")

        # - Publisher
        self.__is_active_publisher = rospy.Publisher('~is_active',
                                                     Bool, queue_size=5)
        rospy.Timer(rospy.Duration(rospy.get_param("~active_publish_rate_sec")), self.__publish_is_active)

        # Jog
        self.__jog_controller = JogController(arm_param_validator)

        # Publish robot state (position, orientation, tool)
        self.__state_publisher = StatePublisher()

        # Set a bool to mentioned this node is initialized
        rospy.set_param('~initialized', True)

        rospy.loginfo("Robot Commander - Started")

    def __start_action_server(self):
        self.__action_server.start()
        rospy.logdebug("Robot Commander - Action Server started")

    # -- CALLBACKS
    # - Subscribers
    def __publish_is_active(self, *_):
        msg = Bool()
        msg.data = self.__current_goal_is_active()
        try:
            self.__is_active_publisher.publish(msg)
        except rospy.ROSException:
            return

    # - Subscribers
    def __callback_learning_mode(self, msg):
        activate = msg.data
        if not self.__learning_mode_on and activate:
            self.__arm_commander.stop_arm()
        self.__learning_mode_on = activate

    def __callback_hardware_status(self, msg):
        self.__hardware_status = msg

    # - Services
    def __callback_stop_command(self, _):
        self.__cancel_command()
        return CommandStatus.SUCCESS, "Command stopped"

    def __callback_is_active(self, _):
        return self.__current_goal_is_active()

    def __callback_pause_movement(self, msg):
        self.__pause_state = msg.state
        if msg.state == PausePlanExecution.PAUSE:
            rospy.loginfo("Robot Commander - Receive Set Pause Mode from button")
            self.__pause_finished_event.clear()
            self.__cancel_command()
        elif msg.state == PausePlanExecution.CANCEL:
            self.__pause_finished_event.set()
            self.__cancel_command()
            rospy.loginfo("Robot Commander - Receive Cancel Command from button")
        else:
            self.__pause_finished_event.set()

    # - Action Server
    def __callback_goal(self, goal_handle):
        """
        This function verifies that all conditions are met in order to execute the request
        :param goal_handle: object use to communicate with the action server
        :return: None
        """
        rospy.loginfo("Commander Action Serv - Received goal. Check if can be executed")

        # Check if hw status has been received at least once
        if self.__hardware_status is None:
            result = self.create_result(CommandStatus.HARDWARE_NOT_OK,
                                        "Hardware Status still not received, please restart the robot")
            goal_handle.set_rejected(result)
            return

        # Check if motor connection problem
        if not self.__hardware_status.connection_up:
            result = self.create_result(CommandStatus.HARDWARE_NOT_OK,
                                        "Motor connection problem, you can't send a command now")
            goal_handle.set_rejected(result)
            return

        # Check if calibration is needed
        if self.__hardware_status.calibration_needed:
            result = self.create_result(CommandStatus.CALIBRATION_NOT_DONE,
                                        "You need to calibrate the robot before sending a command")
            goal_handle.set_rejected(result)
            return

        # Check if calibration is in progress
        if self.__hardware_status.calibration_in_progress:
            result = self.create_result(CommandStatus.CALIBRATION_NOT_DONE,
                                        "Calibration in progress, wait until it ends to send a command")
            goal_handle.set_rejected(result)
            return

        # Check if jog controller enabled
        if self.__jog_controller.is_enabled():
            result = self.create_result(CommandStatus.JOG_CONTROLLER_ENABLED,
                                        "You need to deactivate jog controller to execute a new command")
            goal_handle.set_rejected(result)
            return

        # check if still have a goal
        if self.__current_goal_is_active():
            # If still have a goal, wait a bit to be sure it's not goal is still active
            # due to concurrency issue
            for i in range(self.__command_still_active_max_tries):
                rospy.logwarn("Commander Action Serv - Current goal seems to be still active, "
                              "will retry {} time(s)".format(self.__command_still_active_max_tries - i))
                rospy.sleep(0.2)
                if not self.__current_goal_is_active():
                    break
            else:
                result = self.create_result(CommandStatus.GOAL_STILL_ACTIVE,
                                            "Current command is still active")
                goal_handle.set_rejected(result)
                return

        # Check if learning mode ON
        if self.__learning_mode_on and self.goal_to_cmd_type(goal_handle) != RobotCommand.TOOL_ONLY:
            if not self.__set_learning_mode(False):
                result = self.create_result(CommandStatus.LEARNING_MODE_ON,
                                            "Learning mode could not be deactivated")
                goal_handle.set_rejected(result)
                return
        # set accepted
        self.__current_goal_handle = goal_handle
        self.__current_goal_handle.set_accepted()
        rospy.loginfo("Commander Action Serv - Goal has been accepted")

        # Launch compute + execution in a new thread
        self.__action_server_thread = threading.Thread(target=self.__execute_goal_action,
                                                       name="worker_execute_goal_action")
        self.__action_server_thread.start()
        rospy.logdebug("Commander Action Serv - Executing command in a new thread")

    def __callback_cancel(self, goal_handle):
        rospy.loginfo("Commander Action Serv - Received cancel command")

        if goal_handle == self.__current_goal_handle:
            self.__cancel_current_command()
        else:
            rospy.logdebug("Commander Action Serv - No current goal, nothing to do")

    # -- EXECUTORS
    def __reset_pause_play_state(self):
        self.__pause_finished_event.set()
        self.__pause_state = PausePlanExecution.STANDBY

    def __current_goal_is_active(self):
        if not self.__current_goal_handle.goal:
            return False
        return self.__current_goal_handle.get_goal_status().status in [GoalStatus.ACTIVE, GoalStatus.PENDING]

    def __cancel_due_to_pause(self):
        # Check if plan is paused
        if not self.__pause_finished_event.wait(timeout=self.__pause_timeout):
            self.__current_goal_handle.set_canceled()
            rospy.logwarn("Commander Action Serv - Goal has been paused since too long, "
                          "cancelling it")
            return True

        if self.__pause_state == PausePlanExecution.CANCEL:
            self.__current_goal_handle.set_canceled()
            rospy.loginfo("Commander Action Serv - Goal has been successfully canceled")
            return True

        return False

    def __execute_goal_action(self):
        """
        Threaded function which interpret and execute the command
        It waits until the action finished and set goal_handle according to the result
        :return: None
        """
        if self.__cancel_due_to_pause():
            self.__reset_pause_play_state()
            return
        try:
            cmd = self.__current_goal_handle.goal.goal.cmd
            (status, message) = self.__interpret_and_execute_command(cmd)
            response = self.create_result(status, message)
            result = response
        except (RobotCommanderException, ArmCommanderException, ToolCommanderException) as e:
            result = self.create_result(e.status, e.message)
            response = None
            rospy.loginfo("Commander Action Serv - An exception was "
                          "thrown during command execution : {}".format(e.message))

        # Check if plan is paused and should be restarted
        if self.__pause_state == PausePlanExecution.PAUSE:
            if self.__cancel_due_to_pause():
                self.__reset_pause_play_state()
            elif self.__pause_state == PausePlanExecution.RESUME:
                rospy.loginfo("Commander Action Serv - Resuming goal action")
                return self.__execute_goal_action()
            else:
                self.__current_goal_handle.set_aborted(result)
                rospy.logwarn("Commander Action Serv - Unknown result, goal has been set as aborted")
        # Check response
        elif not response:
            self.__current_goal_handle.set_aborted(result)
            rospy.logwarn("Commander Action Serv - Execution has been aborted")
        elif response.status == CommandStatus.SUCCESS:
            self.__current_goal_handle.set_succeeded(result)
            rospy.loginfo("Commander Action Serv - Goal has been set as succeeded")
        elif response.status == CommandStatus.STOPPED:
            self.__current_goal_handle.set_canceled(result)
            rospy.loginfo("Commander Action Serv - Goal has been successfully canceled")
        elif response.status == CommandStatus.CONTROLLER_PROBLEMS:
            self.__current_goal_handle.set_aborted(result)
            rospy.logwarn("Commander Action Serv - Controller failed during execution : " +
                          "Goal has been aborted.\n" +
                          "This is due to either a collision, or a motor unable to follow a given command" +
                          " (overload, extreme positions, ...)")
        else:
            self.__current_goal_handle.set_aborted(result)
            rospy.logwarn("Commander Action Serv - Unknown result, goal has been set as aborted")

        self.__pause_finished_event.set()

    def __interpret_and_execute_command(self, cmd):
        """
        Take a Robot command, give it to Arm or Tools commander, and return the result
        :param cmd: RobotCommand (see niryo_robot_msgs)
        :return: status, message
        """
        cmd_type = cmd.cmd_type

        if cmd_type == RobotCommand.MOVE_ONLY:
            arm_cmd = cmd.arm_cmd
            arm_cmd_type = arm_cmd.cmd_type
            # noinspection PyArgumentList
            return self.dict_interpreter_move_cmd[arm_cmd_type](arm_cmd)

        elif cmd_type == RobotCommand.TOOL_ONLY:
            return self.__tool_commander.send_tool_command(cmd.tool_cmd)

        else:
            return CommandStatus.UNKNOWN_COMMAND, "Commander Action Serv - Unknown command"

    def __cancel_command(self):
        self.__arm_commander.stop_current_plan()  # Send a cancel signal to Moveit interface
        self.__tool_commander.stop_tool_command()

    @staticmethod
    def __set_learning_mode(set_bool):
        try:
            rospy.wait_for_service('/niryo_robot/learning_mode/activate', timeout=1)
            srv = rospy.ServiceProxy('/niryo_robot/learning_mode/activate', SetBool)
            resp = srv(set_bool)
            return resp.status == CommandStatus.SUCCESS
        except (rospy.ServiceException, rospy.ROSException):
            return False

    @staticmethod
    def create_result(status, message):
        """
        Create a RobotMoveResult object which can be send to the Action Server
        :param status:
        :param message:
        :return: RobotMoveResult object
        """
        result = RobotMoveResult()
        result.status = status
        result.message = message
        return result

    def __cancel_current_command(self):
        try:
            self.__cancel_command()
        except RobotCommanderException:
            rospy.logwarn("Commander Action Serv - Could not cancel current command ")

    @staticmethod
    def goal_to_cmd_type(goal_handle):
        return goal_handle.goal.goal.cmd.cmd_type


# -- STATE PUBLISHER

class StatePublisher:
    """
    This object read Transformation Publisher and Publish the RobotState
     in the Topic '/niryo_robot/robot_state' at a certain rate
    """

    def __init__(self):

        # Tf listener (position + rpy) of end effector tool
        self.__position = [0, 0, 0]
        self.__quaternion = [0, 0, 0, 0]
        self.__rpy = [0, 0, 0]
        self.__tf_listener = tf.TransformListener()

        # State publisher
        self.__robot_state_publisher = rospy.Publisher(
            '/niryo_robot/robot_state', RobotState, queue_size=5)

        # Get params from rosparams
        rate_publish_state = rospy.get_param("/niryo_robot/robot_state/rate_publish_state")

        rospy.Timer(rospy.Duration(1.0 / rate_publish_state), self.__publish_state)

    def __update_ee_link_pose(self):
        try:
            (pos, rot) = self.__tf_listener.lookupTransform('base_link', 'tool_link', rospy.Time(0))
            self.__position = pos
            self.__quaternion = rot
            self.__rpy = tf.transformations.euler_from_quaternion(rot)
        except (LookupException, ConnectivityException, ExtrapolationException):
            rospy.loginfo_throttle(1, "State Publisher - Failed to get TF base_link -> ee_link")

    def __publish_state(self, _):
        self.__update_ee_link_pose()
        msg = RobotState()
        msg.position.x = self.__position[0]
        msg.position.y = self.__position[1]
        msg.position.z = self.__position[2]
        msg.rpy.roll = self.__rpy[0]
        msg.rpy.pitch = self.__rpy[1]
        msg.rpy.yaw = self.__rpy[2]
        msg.orientation.x = self.__quaternion[0]
        msg.orientation.y = self.__quaternion[1]
        msg.orientation.z = self.__quaternion[2]
        msg.orientation.w = self.__quaternion[3]
        try:
            self.__robot_state_publisher.publish(msg)
        except rospy.ROSException:
            return

    @staticmethod
    def get_orientation_from_angles(r, p, y):
        quaternion = quaternion_from_euler(r, p, y)
        orientation = Quaternion()
        orientation.x = quaternion[0]
        orientation.y = quaternion[1]
        orientation.z = quaternion[2]
        orientation.w = quaternion[3]
        return orientation

    @staticmethod
    def get_rpy_from_quaternion(rot):
        euler = tf.transformations.euler_from_quaternion(rot)
        # Force angles in [-PI, PI]
        for i, angle in enumerate(euler):
            if angle > pi:
                euler[i] = angle % (2 * pi) - 2 * pi
            elif angle < -pi:
                euler[i] = angle % (2 * pi)
        return euler


if __name__ == '__main__':
    rospy.init_node('niryo_robot_commander', anonymous=False, log_level=rospy.INFO)
    try:
        node = RobotCommanderNode()
        rospy.spin()
    except rospy.ROSInterruptException:
        pass
