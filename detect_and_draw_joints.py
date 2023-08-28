#!/usr/bin/env python
# -*- coding: utf-8 -*-

import numpy as np
import cv2
import rospy
import argparse
import glob
import time
import math
import tf
import tf.transformations as tr

from lib_draw_3d_joints import Human, set_default_params
from lib_openpose_detector import OpenposeDetector

if True:  # Add project root
    import sys
    import os
    ROOT = os.path.dirname(os.path.abspath(__file__))+'/'
    sys.path.append(ROOT)
    from utils.lib_rgbd import RgbdImage, MyCameraInfo
    from utils.lib_ros_rgbd_pub_and_sub import ColorImageSubscriber, DepthImageSubscriber, CameraInfoSubscriber
    from utils.lib_geo_trans import rotx, roty, rotz, get_Rp_from_T, form_T


def parse_command_line_arguments():

    parser = argparse.ArgumentParser(
        description="Detect human joints and then draw in rviz.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    # -- Select data source.
    parser.add_argument("-s", "--data_source",
                        default="disk",
                        choices=["rostopic", "disk"])
    parser.add_argument("-z", "--detect_hand", type=Bool,
                        default=False)
    parser.add_argument("-u", "--depth_unit", type=float,
                        default="0.001",
                        help="Depth is (pixel_value * depth_unit) meters.")
    parser.add_argument("-r", "--is_using_realsense", type=Bool,
                        default=False,
                        help="If the data source is Realsense, set this to true. "
                        "Then, the drawn joints will change the coordinate to be the same as "
                        "Realsense's point cloud. The reason is,"
                        "I used a different coordinate direction than Realsense."
                        "(1) For me, I use X-Right, Y-Down, Z-Forward,"
                        "which is the convention for camera."
                        "(2) For Realsense ROS package, it's X-Forward, Y-Left, Z-Up.")

    # -- "rostopic" as data source.
    parser.add_argument("-a", "--ros_topic_color",
                        default="overhead/color/image_raw")
    parser.add_argument("-b", "--ros_topic_depth",
                        default="overhead/aligned_depth_to_color/image_raw")
    parser.add_argument("-c", "--ros_topic_camera_info",
                        default="overhead/color/camera_info")

    # -- "disk" as data source.
    parser.add_argument("-d", "--base_folder",
                        default=ROOT)
    parser.add_argument("-e", "--folder_color",
                        default="data/images_n40/color/")
    parser.add_argument("-f", "--folder_depth",
                        default="data/images_n40/depth/")
    parser.add_argument("-g", "--camera_info_file",
                        default="data/images_n40/cam_params_realsense.json")

    # -- Get args.
    inputs = rospy.myargv()[1:]
    inputs = [s for s in inputs if s.replace(" ", "") != ""]  # Remove blanks.
    args = parser.parse_args(inputs)

    # -- Deal with relative path.
    b = args.base_folder + "/"
    args.folder_color = b + args.folder_color
    args.folder_depth = b + args.folder_depth
    args.camera_info_file = b + args.camera_info_file

    # -- Return
    return args


def Bool(v):
    ''' A bool class for argparser '''
    # TODO: Add a reference
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


class DataReader_DISK(object):
    def __init__(self, args):
        self._fcolors = sorted(glob.glob(args.folder_color + "/*"))
        basenames = [os.path.basename(s) for s in self._fcolors]
        self._fdepths = [args.folder_depth + "/" + s for s in basenames]
        self._camera_info = MyCameraInfo(
            camera_info_file_path=args.camera_info_file)
        self._depth_unit = args.depth_unit
        self._cnt_imgs = 0
        self._total_images = len(self._fcolors)

    def total_images(self):
        return self._total_images

    def read_next_data(self):
        def read_img(folders, i):
            return cv2.imread(folders[i], cv2.IMREAD_UNCHANGED)
        color = read_img(self._fcolors, self._cnt_imgs)
        depth = read_img(self._fdepths, self._cnt_imgs)
        self._cnt_imgs += 1
        rgbd = RgbdImage(color, depth,
                         self._camera_info,
                         depth_unit=self._depth_unit)
        return rgbd


class DataReader_ROS(object):
    def __init__(self, args):
        self._sub_c = ColorImageSubscriber(args.ros_topic_color)
        self._sub_d = DepthImageSubscriber(args.ros_topic_depth)
        self._sub_i = CameraInfoSubscriber(args.ros_topic_camera_info)
        self._depth_unit = args.depth_unit
        self._camera_info = None
        self._cnt_imgs = 0

    def _get_camera_info(self):
        '''
        Since camera info usually doesn't change,
        we read it from cache after it's initialized.
        '''
        if self._camera_info is None:
            while (not self._sub_i.has_camera_info()) and (not rospy.is_shutdown):
                rospy.sleep(0.001)
            if self._sub_i.has_camera_info:
                self._camera_info = MyCameraInfo(
                    ros_camera_info=self._sub_i.get_camera_info())
        return self._camera_info

    def total_images(self):
        ''' Set a large number here. '''
        return 9999

    def _read_depth(self):
        while not self._sub_d.has_image() and (not rospy.is_shutdown()):
            rospy.sleep(0.001)
        depth = self._sub_d.get_image()
        return depth

    def _read_color(self):
        while not self._sub_c.has_image() and (not rospy.is_shutdown()):
            rospy.sleep(0.001)
        color = self._sub_c.get_image()
        return color

    def read_next_data(self):
        depth = self._read_depth()
        color = self._read_color()
        camera_info = self._get_camera_info()
        self._cnt_imgs += 1
        rgbd = RgbdImage(color, depth,
                         camera_info,
                         depth_unit=self._depth_unit)
        return rgbd


def main(args):

    # -- Data reader.
    if args.data_source == "disk":
        data_reader = DataReader_DISK(args)
    else:
        data_reader = DataReader_ROS(args)
    ith_image = 0
    total_images = data_reader.total_images()

    # -- Detector.
    detector = OpenposeDetector(
        {"hand": args.detect_hand})

    p = [0.70708003, 0.34563621, 2.35865171]
    cam_pose_tf = np.array([[-0.05002631,  0.98019736, -0.19159987,  0.        ],
                    [ 0.99872766,  0.05031745, -0.00334879,  0.        ],
                    [ 0.00635834, -0.19152362, -0.98146741,  0.        ],
                    [ 0.,          0.,          0.,          1.        ]])
    cam_pose_tf[0:3, -1] = p

    # -- Settings.
    cam_pose, cam_pose_pub = set_default_params()
    cam_pose = cam_pose_tf
    # listener = tf.TransformListener()
    if args.is_using_realsense: # Change coordinate.
        R, p = get_Rp_from_T(cam_pose)
        R = roty(math.pi/2).dot(rotz(-math.pi/2)).dot(R)
        # R = roty(math.pi/2).dot(rotz(-math.pi/2 + 0.07)).dot(R)
        # R = rotx(math.pi/2).dot(roty(math.pi)).dot(R)
        # R = roty(0.15).dot(R)
        # p = [0.61, 0.4868, 2.3578798]
        cam_pose = form_T(R, p)

    # -- Loop: read, detect, draw.
    prev_humans = []
    while not rospy.is_shutdown():
        t0 = time.time()

        # -- Read data
        print("============================================")
        rospy.loginfo("Reading {}/{}th color/depth images...".format(
            ith_image+1, total_images))
        rgbd = data_reader.read_next_data()
        rgbd.set_camera_pose(cam_pose)
        ith_image += 1

        # -- Detect joints.
        print("  Detecting joints...")
        body_joints, hand_joints = detector.detect(
            rgbd.color_image(), is_return_joints=True)
        N_people = len(body_joints)

        # -- Delete previous joints.
        for human in prev_humans:
            # If I put delete after drawing new markders,
            # The delete doesn't work. I don't know why.
            human.delete_rviz()

        # -- Draw humans in rviz.
        humans = []
        for i in range(N_people):
            human = Human(rgbd, body_joints[i], hand_joints[i])
            human.draw_rviz()
            rospy.loginfo("  Drawing {}/{}th person with id={} on rviz.".format(
                i+1, N_people, human._id))
            rospy.loginfo("    " + human.get_hands_str())
            humans.append(human)
        # publish the right arm poses
        if len(humans) > 0:
            human = humans[0]
            human.publish_right_arm_pose()
        prev_humans = humans
        print("Total time = {} seconds.".format(time.time()-t0))

        # -- Keep update camera pose for rviz visualization.
        # cam_pose_pub.publish()

        # -- Reset data.
        if args.data_source == "disk" and ith_image == total_images:
            data_reader = DataReader_DISK(args)
            ith_image = 0

    # -- Clean up.
    for human in humans:
        human.delete_rviz()


if __name__ == '__main__':
    node_name = "detect_and_draw_joints"
    rospy.init_node(node_name)
    rospy.sleep(0.1)
    args = parse_command_line_arguments()
    main(args)
    rospy.logwarn("Node `{}` stops.".format(node_name))
