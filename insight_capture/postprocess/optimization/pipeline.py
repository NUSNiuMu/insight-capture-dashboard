"""Extract a standard MCAP and run the existing COLMAP reconstruction tools."""

import argparse
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from insight_capture.core.bag import iter_messages, topic_types
from insight_capture.media.h264_decode import H264Decoder
from insight_capture.media.ros_image import decode_color_image, header_stamp_ns


def camera_reference(bag, vio_topic, info_topic, image_topic, output):
    from geometry_msgs.msg import PoseStamped
    from rclpy.serialization import deserialize_message
    from rclpy.time import Time
    from sensor_msgs.msg import CameraInfo
    from tf2_msgs.msg import TFMessage
    from tf2_ros import Buffer

    transforms = Buffer()
    poses, info = [], None
    for topic, raw, _ in iter_messages(bag, [vio_topic, info_topic, '/tf_static']):
        if topic == vio_topic:
            poses.append(deserialize_message(raw, PoseStamped))
        elif topic == info_topic:
            info = deserialize_message(raw, CameraInfo)
        else:
            for transform in deserialize_message(raw, TFMessage).transforms:
                transforms.set_transform_static(transform, 'recording')
    if info is None or not poses:
        raise ValueError('Recorded CameraInfo and VIO are required.')
    prefix = vio_topic.split('/camera/', 1)[0].strip('/')
    imu = prefix + '_camera_imu'
    if not transforms.can_transform(imu, info.header.frame_id, Time()):
        raise ValueError(f'Recording is missing TF: {imu} <- {info.header.frame_id}.')
    transform = transforms.lookup_transform(imu, info.header.frame_id, Time()).transform
    q, t = transform.rotation, transform.translation
    camera_rotation = Rotation.from_quat([q.x, q.y, q.z, q.w]).as_matrix()
    camera_translation = np.array([t.x, t.y, t.z])
    if 'image_rect' in image_topic:
        P = np.asarray(info.p).reshape(3, 4)
        model, params = 'PINHOLE', [P[0, 0], P[1, 1], P[0, 2], P[1, 2]]
        camera_rotation = camera_rotation @ np.asarray(info.r).reshape(3, 3).T
    else:
        K = np.asarray(info.k).reshape(3, 3)
        params = [K[0, 0], K[1, 1], K[0, 2], K[1, 2]]
        if info.distortion_model == 'equidistant':
            model, params = 'OPENCV_FISHEYE', params + list(info.d)
        elif info.distortion_model in {'plumb_bob', 'rational_polynomial'}:
            model, params = 'FULL_OPENCV', params + list(info.d) + [0.] * (8-len(info.d))
        else:
            raise ValueError(f'Unsupported recorded distortion model: {info.distortion_model}')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w') as stream:
        for pose in poses:
            stamp = header_stamp_ns(pose)
            if stamp <= 0:
                raise ValueError('VIO has an invalid source timestamp.')
            p, q = pose.pose.position, pose.pose.orientation
            rotation = Rotation.from_quat([q.x, q.y, q.z, q.w]).as_matrix()
            position = np.array([p.x, p.y, p.z]) + rotation @ camera_translation
            orientation = Rotation.from_matrix(rotation @ camera_rotation).as_quat()
            stream.write(f'{stamp//10**9}.{stamp%10**9:09d} ' +
                         ' '.join(f'{value:.9f}' for value in (*position, *orientation)) + '\n')
    print(f'Extracted {len(poses)} camera-centre VIO poses', flush=True)
    return model, params, (info.width, info.height)


def extract_images(bag, topic, output, hz):
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    kind = topic_types(bag)[topic]
    message_type = get_message(kind)
    output.mkdir(parents=True, exist_ok=True)
    count, next_stamp, shape = 0, 0, None
    decoder, stamps, selected = None, {}, {}

    def write(stamp, frame):
        nonlocal count, shape
        if frame is None:
            raise ValueError('Recorded image could not be decoded.')
        dimensions = (frame.shape[1], frame.shape[0])
        if shape is not None and shape != dimensions:
            raise ValueError('Image dimensions changed within the recording.')
        shape = dimensions
        count += 1
        name = f'{count:06d}_{stamp//10**9}.{stamp%10**9:09d}.png'
        if not cv2.imwrite(str(output/name), frame):
            raise OSError('Cannot write extracted image.')
        if count % 25 == 0:
            print(f'Extracted {count} images', flush=True)

    def drain(wait=0):
        for pts in decoder.take_discarded_timestamps():
            stamps.pop(pts, None)
            selected.pop(pts, None)
        result = decoder.pull(wait)
        while result is not None:
            pts, frame = result
            write(stamps.pop(pts), frame)
            selected.pop(pts)
            result = decoder.pull()

    try:
        for index, (_, raw, _) in enumerate(iter_messages(bag, [topic])):
            message = deserialize_message(raw, message_type)
            stamp = header_stamp_ns(message)
            if stamp <= 0:
                raise ValueError('Image has an invalid source timestamp.')
            keep = stamp >= next_stamp
            if keep:
                next_stamp = stamp + round(10**9/hz)
            if kind.endswith('CompressedImage') and message.format.lower() == 'h264':
                if decoder is None:
                    decoder = H264Decoder(pixel_format='GRAY8', frame_selector=lambda pts: selected[pts])
                pts = index * 10**9
                stamps[pts], selected[pts] = stamp, keep
                if not decoder.push(bytes(message.data), pts):
                    stamps.pop(pts); selected.pop(pts)
                drain(20_000_000)
            elif keep:
                frame = decode_color_image(message, kind)
                if frame is not None and frame.ndim == 3:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                write(stamp, frame)
        if decoder is not None:
            decoder.finish()
            deadline = time.monotonic() + 10
            while not decoder.sink.get_property('eos') and time.monotonic() < deadline:
                drain(100_000_000)
            if not decoder.sink.get_property('eos'):
                raise RuntimeError('H.264 decoder did not finish.')
    finally:
        if decoder is not None:
            decoder.close()
    if not count:
        raise ValueError('No decodable images were extracted.')
    print(f'Extracted {count} images at up to {hz:g} Hz, dimensions={shape}', flush=True)
    return shape


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bag', type=Path, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--vio-topic', required=True)
    parser.add_argument('--image-topic', required=True)
    parser.add_argument('--info-topic', required=True)
    parser.add_argument('--tools-dir', type=Path, required=True)
    parser.add_argument('--output-hz', type=float, default=5.)
    args = parser.parse_args()
    if Path(args.name).name != args.name or args.name in {'.', '..'}:
        raise ValueError('Run name must be a directory name.')
    if not math.isfinite(args.output_hz) or args.output_hz <= 0:
        raise ValueError("Output frequency must be positive and finite.")
    derived = Path('data/derived')/args.name
    run = Path('runs')/args.name
    for directory in (derived, run):
        if directory.exists():
            shutil.rmtree(directory)
    label = f'images_{args.output_hz:g}hz'
    vio = derived/'trajectories/vio_100hz.tum'
    images = derived/'images'/label
    colmap = run/'colmap'/label
    aligned = run/'viz'/(label+'_vs_vio100')/'colmap_sim3.tum'
    print('STEP 1: Extracting VIO', flush=True)
    model, params, calibrated = camera_reference(args.bag, args.vio_topic, args.info_topic, args.image_topic, vio)
    print('STEP 2: Extracting images', flush=True)
    size = extract_images(args.bag, args.image_topic, images, args.output_hz)
    for i in (0, 2): params[i] *= size[0]/calibrated[0]
    for i in (1, 3): params[i] *= size[1]/calibrated[1]
    print('STEP 3: Running COLMAP', flush=True)
    subprocess.run([sys.executable, '-u', str(args.tools_dir/'run_colmap_from_images.py'),
                    '--image-dir', str(images), '--output-dir', str(colmap),
                    '--colmap-runner', 'local', '--use-gpu', 'true',
                    '--camera-model', model, '--camera-params', ','.join(map(str, params)),
                    '--overwrite', 'true'], check=True)
    print('STEP 4: Aligning trajectories', flush=True)
    subprocess.run([sys.executable, '-u', str(args.tools_dir/'align_colmap_to_vio.py'),
                    '--vio', str(vio), '--colmap', str(colmap/'colmap.tum'),
                    '--output', str(aligned), '--max-time-diff', '.01', '--one-to-one', 'true'], check=True)
    print(f'Complete: {aligned}', flush=True)


if __name__ == '__main__':
    main()
