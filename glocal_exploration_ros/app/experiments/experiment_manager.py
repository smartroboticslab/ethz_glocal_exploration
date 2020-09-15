#!/usr/bin/env python

# Python
import sys
import time
import csv
import datetime
import os
import re
import subprocess
import math
import numpy as np

# ros
import rospy
import tf

# msgs
from std_msgs.msg import Bool
from std_srvs.srv import SetBool
from voxblox_msgs.srv import FilePath


class EvalData(object):
    def __init__(self):
        '''  Initialize ros node and read params '''
        # Parse parameters
        self.ns_planner = rospy.get_param(
            '~ns_planner', "/glocal/glocal_system/toggle_running")
        self.planner_delay = rospy.get_param(
            '~delay', 0.0)  # Waiting time until the planner is launched
        self.startup_timeout = rospy.get_param(
            '~startup_timeout', 0.0)  # Max allowed time for startup, 0 for inf

        self.evaluate = rospy.get_param(
            '~evaluate', False)  # Periodically save the voxblox state
        self.eval_frequency = rospy.get_param('~eval_frequency',
                                              30.0)  # Save rate in seconds
        self.time_limit = rospy.get_param(
            '~time_limit', 0.0)  # Maximum sim duration in minutes, 0 for inf

        self.eval_walltime_0 = None
        self.eval_rostime_0 = None
        self.shutdown_reason_known = False

        if self.evaluate:
            # Setup parameters
            self.eval_directory = rospy.get_param(
                '~eval_directory',
                'DirParamNotSet')  # Periodically save voxblox map
            if not os.path.isdir(self.eval_directory):
                rospy.logfatal("Invalid target directory '%s'.",
                               self.eval_directory)
                sys.exit(-1)

            self.ns_voxblox = rospy.get_param('~ns_voxblox',
                                              "/voxblox/voxblox_node")

            # Statistics
            self.eval_n_maps = 0
            self.collided = False
            self.run_planner_srv = None

            # Setup data directory
            if not os.path.isdir(os.path.join(self.eval_directory,
                                              "tmp_bags")):
                os.mkdir(os.path.join(self.eval_directory, "tmp_bags"))
            self.eval_directory = os.path.join(
                self.eval_directory,
                datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
            os.mkdir(self.eval_directory)
            os.mkdir(os.path.join(self.eval_directory, "voxblox_maps"))
            self.eval_data_file = open(
                os.path.join(self.eval_directory, "voxblox_data.csv"), 'wb')
            self.eval_writer = csv.writer(self.eval_data_file,
                                          delimiter=',',
                                          quotechar='|',
                                          quoting=csv.QUOTE_MINIMAL,
                                          lineterminator='\n')
            self.eval_writer.writerow([
                'MapName', 'RosTime', 'WallTime', 'PositionDrift',
                'RotationDrift', 'PositionDriftEstimated',
                'RotationDriftEstimated'
            ])
            self.eval_writer.writerow(
                ['Unit', 's', 's', 'm', 'deg', 'm', 'deg'])
            self.eval_log_file = open(
                os.path.join(self.eval_directory, "data_log.txt"), 'a')

            # Subscribers, Services
            self.collision_sub = rospy.Subscriber("collision",
                                                  Bool,
                                                  self.collision_callback,
                                                  queue_size=10)
            self.tf_listener = tf.TransformListener()

            # Finish
            self.writelog("Data folder created at '%s'." % self.eval_directory)
            rospy.loginfo("[ExperimentManager]: Data folder created at '%s'." %
                          self.eval_directory)
            self.eval_voxblox_service = rospy.ServiceProxy(
                self.ns_voxblox + "/save_map", FilePath)
            rospy.on_shutdown(self.eval_finish)

        self.launch_simulation()

    def launch_simulation(self):
        rospy.loginfo(
            "[ExperimentManager]: Waiting for unreal MAV simulation to setup..."
        )
        # Wait for unreal simulation to setup
        if self.startup_timeout > 0.0:
            try:
                rospy.wait_for_message("/simulation_is_ready", Bool,
                                       self.startup_timeout)
            except rospy.ROSException:
                self.stop_experiment(
                    "Simulation startup failed (timeout after " +
                    str(self.startup_timeout) + "s).")
                return
        else:
            rospy.wait_for_message("/simulation_is_ready", Bool)
        rospy.loginfo(
            "[ExperimentManager]: Waiting for unreal MAV simulation to setup..."
            " done.")

        # Launch planner
        # (every planner needs to advertise this service when ready)
        rospy.loginfo(
            "[ExperimentManager]: Waiting for planner to be ready...")
        if self.startup_timeout > 0.0:
            try:
                rospy.wait_for_service(self.ns_planner, self.startup_timeout)
            except rospy.ROSException:
                self.stop_experiment("Planner startup failed (timeout after " +
                                     str(self.startup_timeout) + "s).")
                return
        else:
            rospy.wait_for_service(self.ns_planner)

        if self.planner_delay > 0:
            rospy.loginfo(
                "[ExperimentManager]: Waiting for planner to be ready... done. "
                "Launch in %d seconds.", self.planner_delay)
            rospy.sleep(self.planner_delay)
        else:
            rospy.loginfo(
                "[ExperimentManager]: Waiting for planner to be ready... done."
            )
        self.run_planner_srv = rospy.ServiceProxy(self.ns_planner, SetBool)
        self.run_planner_srv(True)

        # Setup first measurements
        self.eval_walltime_0 = time.time()
        self.eval_rostime_0 = rospy.get_time()
        # Evaluation init
        if self.evaluate:
            self.writelog("Succesfully started the simulation.")

            # Dump complete rosparams for reference
            subprocess.check_call([
                "rosparam", "dump",
                os.path.join(self.eval_directory, "rosparams.yaml"), "/"
            ])
            self.writelog("Dumped the parameter server into 'rosparams.yaml'.")

            self.eval_n_maps = 0

            # Keep track of the (most recent) rosbag
            bag_expr = re.compile(
                r'tmp_bag_\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.bag.'
            )  # Default names
            bags = [
                b for b in os.listdir(
                    os.path.join(os.path.dirname(self.eval_directory),
                                 "tmp_bags")) if bag_expr.match(b)
            ]
            bags.sort(reverse=True)
            if bags:
                self.writelog("Registered '%s' as bag for this experiment." %
                              bags[0])
                self.eval_log_file.write("[FLAG] Rosbag: %s\n" %
                                         bags[0].split('.')[0])
            else:
                rospy.logwarn(
                    "[ExperimentManager]: No tmpbag found. Is rosbag recording?"
                )
                self.writelog("No rosbag found to register.")

        # Periodic evaluation (call once for initial measurement)
        self.eval_callback(None)
        rospy.Timer(rospy.Duration(self.eval_frequency), self.eval_callback)

        # Finish
        rospy.loginfo("\n" + "*" * 40 +
                      "\n* Successfully started the experiment! *\n" +
                      "*" * 40)

    def eval_callback(self, _):
        if self.evaluate:
            # Check whether the planner is still alive
            try:
                # If planner is running calling this service again does nothing
                self.run_planner_srv(True)
            except:
                # Usually this means the planner died
                self.stop_experiment("Planner Node died.")
                return

            # Produce a data point
            time_real = time.time() - self.eval_walltime_0
            time_ros = rospy.get_time() - self.eval_rostime_0
            map_name = "{0:05d}".format(self.eval_n_maps)

            # Compute transform errors
            drift_pos = None
            drift_rot = 0
            try:
                (t, r) = self.tf_listener.lookupTransform(
                    'airsim_drone/Lidar', 'airsim_drone/Lidar_ground_truth',
                    rospy.Time(0))
            except (tf.LookupException, tf.ConnectivityException,
                    tf.ExtrapolationException):
                drift_pos = 0
            if drift_pos is None:
                drift_pos = (t[0]**2 + t[1]**2 + t[2]**2)**0.5
                drift_rot = 2 * math.acos(r[3]) * 180.0 / math.pi

            drift_estimated_pos = None
            drift_estimated_rot = 0
            try:
                (t,
                 r) = self.tf_listener.lookupTransform('odom', 'initial_pose',
                                                       rospy.Time(0))
                (t2, r2) = self.tf_listener.lookupTransform(
                    'airsim_drone/Lidar_ground_truth',
                    'airsim_drone_ground_truth', rospy.Time(0))
            except (tf.LookupException, tf.ConnectivityException,
                    tf.ExtrapolationException):
                drift_estimated_pos = 0
            if drift_estimated_pos is None:
                trans1 = np.dot(tf.transformations.translation_matrix(t),
                                tf.transformations.quaternion_matrix(r))
                trans2 = np.dot(tf.transformations.translation_matrix(t2),
                                tf.transformations.quaternion_matrix(r2))

                trans = tf.transformations.concatenate_matrices(trans1, trans2)
                t = tf.transformations.translation_from_matrix(trans)
                r = tf.transformations.quaternion_from_matrix(trans)
                drift_estimated_pos = (t[0]**2 + t[1]**2 + t[2]**2)**0.5
                drift_estimated_rot = 2 * math.acos(r[3]) * 180.0 / math.pi

            self.eval_writer.writerow([
                map_name, time_ros, time_real, drift_pos, drift_rot,
                drift_estimated_pos, drift_estimated_rot
            ])
            self.eval_voxblox_service(
                os.path.join(self.eval_directory, "voxblox_maps",
                             map_name + ".vxblx"))
            self.eval_n_maps += 1

        # If the time limit is reached stop the simulation
        if self.time_limit > 0.0:
            if rospy.get_time(
            ) - self.eval_rostime_0 >= self.time_limit * 60.0:
                self.stop_experiment("Time limit reached.")

    def writelog(self, text):
        # In case of simulation data being stored, maintain a log file
        if not self.evaluate:
            return
        self.eval_log_file.write(
            datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ") + text +
            "\n")

    def stop_experiment(self, reason):
        # Shutdown the node with proper logging, only required when experiment
        # is being performed
        reason = "Stopping the experiment: " + reason
        self.shutdown_reason_known = True
        if self.evaluate:
            self.writelog(reason)
        width = len(reason) + 4
        rospy.loginfo("\n" + "*" * width + "\n* " + reason + " *\n" +
                      "*" * width)
        rospy.signal_shutdown(reason)

    def eval_finish(self):
        self.eval_data_file.close()
        map_path = os.path.join(self.eval_directory, "voxblox_maps")
        n_maps = len([
            f for f in os.listdir(map_path)
            if os.path.isfile(os.path.join(map_path, f))
        ])
        if not self.shutdown_reason_known:
            self.writelog("Stopping the experiment: External Interrupt.")
        self.writelog("Finished the simulation, %d/%d maps created." %
                      (n_maps, self.eval_n_maps))
        self.eval_log_file.close()
        rospy.loginfo(
            "[ExperimentManager]: On eval_data_node shutdown: closing data "
            "files.")

    def collision_callback(self, _):
        if not self.collided:
            self.collided = True
            self.stop_experiment("Collision detected!")


if __name__ == '__main__':
    rospy.init_node('experiment_manager', anonymous=False)
    ed = EvalData()
    rospy.spin()
