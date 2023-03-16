from utils import (
    DOWNLOAD_DIRECTORY,
    enum_ur_fall,
    enum_florence_3d,
    parse_florence_3d_name,
    slice_dataset,
    get_center_of_mass,
    Configs,
    get_landmark,
    putText,
    flatten_landmark,
    Mediapipe_Person,
    Mediapipe_Pose,
    MODEL_TYPES,
    DATASETS,
    INPUT_TYPES,
)
from stream import VideoHandler, VideoStream, FolderHandler
from models import DNNModel, DecisionTree, ManualDecisionTree, XGBoostModel, yolo_detect, yolo_wrap_detection, yolo_draw
from sklearn.metrics import confusion_matrix, classification_report
import cv2
import numpy as np
import math
import matplotlib.pyplot as plt


class DatasetBuffer:
    def __init__(self) -> None:
        self.raw_landmarks = []
        self.numbers = []
        self.dataset = None
        self.folder_name = "undefined"
        self.full_name = "undefined"


def get_urfall_buffer(start, end, configs):
    pose = Mediapipe_Pose()
    buffers = []
    for (folder_name, full_name) in enum_ur_fall(start, end):
        stream = VideoStream(FolderHandler(full_name, configs, suffix=".png"), configs)
        numbers = []
        with open(f"markers/{folder_name}.txt", "r") as f:
            numbers = [int(num) for line in f for num in line.strip().split()]
        buffer = DatasetBuffer()
        buffer.dataset = DATASETS.ur_fall
        buffer.numbers = numbers

        buffer.folder_name = folder_name
        buffer.full_name = full_name
        while True:
            img = stream.get_image()
            if img is None:
                break
            results = pose.process(img)
            if results.pose_landmarks is None:
                continue
            landmarks = get_landmark(results.pose_landmarks.landmark, configs)
            buffer.raw_landmarks.append(landmarks)

        buffers.append(buffer)

        stream.dispose()

    return buffers


def get_florence_buffer(start, end, configs):
    pose = Mediapipe_Pose()
    buffers = []
    i = 0
    for (folder_name, full_name) in enum_florence_3d():
        idGesture, idActor, idAction, idCategory = parse_florence_3d_name(folder_name)
        if idCategory != 2:
            continue
        i += 1
        if i < start or i >= end:
            continue
        stream = VideoStream(VideoHandler(full_name, configs), configs)
        buffer = DatasetBuffer()
        buffer.dataset = DATASETS.ur_fall
        buffer.numbers = []
        buffer.folder_name = folder_name
        buffer.full_name = full_name
        while True:
            img = stream.get_image()
            if img is None:
                break
            results = pose.process(img)
            buffer.numbers.append(2)
            if results.pose_landmarks is None:
                continue
            landmarks = get_landmark(results.pose_landmarks.landmark, configs)
            buffer.raw_landmarks.append(landmarks)

        buffers.append(buffer)

        stream.dispose()

    return buffers


# left-inclusive, right-exclusive
def mediapipe_dnn_stream(buffers: list[DatasetBuffer], configs: Configs):
    pose = Mediapipe_Pose()
    models = [
        DNNModel(
            configs,
            loadfromfile=not (MODEL_TYPES.Mediapipe_DNN in configs.train),
        ),
        DecisionTree(
            configs,
            loadfromfile=not (MODEL_TYPES.Mediapipe_CLF in configs.train),
        ),
        # ManualDecisionTree(configs, 11.80, test_torso_thres),
        XGBoostModel(
            configs,
            loadfromfile=not (MODEL_TYPES.Mediapipe_XGBoost in configs.train),
        ),
    ]

    x_array_input = []
    x_manual_input = []
    y_array_truth = []
    y_dummy_truth = []  # dummy variable for clf

    for dataset_buffer in buffers:
        aggregate_landmarks = []
        aggregate_manual_input = []
        raw_aggregate_landmarks = []
        numbers = dataset_buffer.numbers

        frame = -1
        last_rec = -1
        for landmarks in dataset_buffer.raw_landmarks:
            frame += 1

            if frame - last_rec > configs.compress_frames:
                last_rec = frame
            else:
                continue
            raw_aggregate_landmarks.append(landmarks)
            aggregate_landmarks.append(flatten_landmark(raw_aggregate_landmarks[len(raw_aggregate_landmarks) - 1]))
            if len(aggregate_landmarks) > configs.consecutive_frame_count:
                aggregate_landmarks.pop(0)
                raw_aggregate_landmarks.pop(0)

            elif len(aggregate_landmarks) < 2:
                continue

            com_0 = get_center_of_mass(aggregate_landmarks[0], configs)
            com_1 = get_center_of_mass(aggregate_landmarks[1], configs)
            torso_0 = (
                raw_aggregate_landmarks[0][11]
                + raw_aggregate_landmarks[0][12]
                + raw_aggregate_landmarks[0][23]
                + raw_aggregate_landmarks[0][24]
            ) / 4
            torso_1 = (
                raw_aggregate_landmarks[1][11]
                + raw_aggregate_landmarks[1][12]
                + raw_aggregate_landmarks[1][23]
                + raw_aggregate_landmarks[1][24]
            ) / 4
            torso_vel = np.linalg.norm(np.subtract(torso_1, torso_0)) * 30 * 100  # cm/s
            com_vel = np.linalg.norm(np.subtract(com_1, com_0)) * 30 * 100  # cm/s
            man_input = (torso_vel, com_vel)

            aggregate_manual_input.append(man_input)
            if len(aggregate_manual_input) > configs.consecutive_frame_count - 1:
                aggregate_manual_input.pop(0)
            elif len(aggregate_manual_input) < configs.consecutive_frame_count - 1:
                continue

            flatten = [val for landmarks in aggregate_landmarks for val in landmarks]
            flat_agg_man = [val for tup in aggregate_manual_input for val in tup]

            y_dummy_truth.append(numbers[frame])
            y_sample = [0] * 11
            y_sample[numbers[frame]] = 1
            y_array_truth.append(y_sample)
            x_manual_input.append(flat_agg_man)

            x_array_input.append(flatten)

    if configs.train != []:
        for i in range(0, len(models)):
            model = models[i]
            if model.type in configs.train:
                model.train(x_manual_input if configs.input_type == INPUT_TYPES.Proc else x_array_input, y_array_truth)
                model.save()
    if configs.test != []:
        target_names = ["no action", "fall", "drink"]
        with open(f"mult3_{str(configs.input_type)}_report_2_cmp{str(configs.compress_frames)}.txt", "a") as f:
            f.write(f"{configs.consecutive_frame_count}\t")
            for i in range(0, len(models)):
                model = models[i]
                model.delete()
                if model.type in configs.test:
                    batch_result = model.predict(
                        x_manual_input if configs.input_type == INPUT_TYPES.Proc else x_array_input
                    )
                    batch_result = [np.argmax(sample) for sample in batch_result]
                    cf = confusion_matrix(y_dummy_truth, batch_result)
                    report = classification_report(
                        y_dummy_truth,
                        batch_result,
                        labels=range(len(target_names)),
                        target_names=target_names,
                        output_dict=True,
                    )
                    percentage = int(configs.train_percentage * 100)
                    f.write(str(report["fall"]["recall"]) + "\t" + str(report["drink"]["recall"]) + "\t")
            f.write("\n")


def multiple_stream(configs):

    pose = Mediapipe_Pose()

    net = cv2.dnn.readNet("yolov5s.onnx")
    stream = VideoStream(VideoHandler(str(DOWNLOAD_DIRECTORY / "video.mp4"), configs), configs)
    frame = -1

    test_people = []
    while True:
        frame += 1
        img = stream.get_image()
        if img is None:
            break
        img = cv2.resize(img, (640, 640))
        img = cv2.flip(img, 1)
        maxheight, maxwidth, _ = img.shape

        predictions = yolo_detect(img, net)

        class_ids, confidences, boxes = yolo_wrap_detection(img, predictions[0])
        yolo_draw(img, class_ids, confidences, boxes)

        people_count = 0

        for (classid, confidence, box) in zip(class_ids, confidences, boxes):
            if classid == 0:
                left, top, width, height = box
                results = pose.process(
                    img[
                        max(0, top - 30) : min(maxheight, top + height + 30),
                        max(0, left - 30) : min(maxwidth, left + width + 30),
                    ]
                )
                if results.pose_landmarks is None:
                    continue
                people_count += 1
                translated = get_landmark(results.pose_landmarks.landmark, configs)
                mass_center = get_center_of_mass(translated)
                rect_center = (top + height / 2, left + width / 2)
                person_id = -1
                for id in range(0, len(test_people)):
                    dist = np.linalg.norm(np.subtract(test_people[id].rect_center, rect_center))
                    if dist < 60:
                        person_id = id
                        break

                if person_id == -1:
                    test_people.append(Mediapipe_Person(rect_center))
                else:
                    test_people[person_id].rect_center = rect_center
                cv2.putText(
                    img,
                    "P" + str(person_id),
                    (round(box[0]) - 10, round(box[1]) - 10),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 0, 0),
                    2,
                )
                for tup in translated:
                    x, y = math.floor(left + tup[0] * width), math.floor(top + tup[1] * height)
                    if x < 0 or y < 0:
                        continue
                    cv2.circle(
                        img,
                        (x, y),
                        2,
                        (180, 255, 255),
                        thickness=6,
                    )
        if people_count == 0:
            results = pose.process(img)
            if results.pose_landmarks is not None:
                for tup in translated:
                    x, y = math.floor(left + tup[0] * width), math.floor(top + tup[1] * height)
                    if x < 0 or y < 0:
                        continue
                    cv2.circle(
                        img,
                        (x, y),
                        2,
                        (180, 255, 255),
                        thickness=6,
                    )

        cv2.imshow("live stream", img)
        if cv2.waitKey(0) & 0xFF == ord("q"):
            # breaking the loop if the user types q
            # note that the video window must be highlighted!
            break

    stream.dispose()


def percentage_1():
    cv2.startWindowThread()
    consecutive_frame_count = 2
    for p in range(10, 81, 10):
        r_train, _ = slice_dataset(1, 30, p / 100)
        print("train: ", r_train)
        mediapipe_dnn_stream(
            r_train[0] + 5,
            r_train[1] + 5,
            Configs(
                render=False,
                train=[x for x in MODEL_TYPES],
                consecutive_frame_count=consecutive_frame_count,
                train_percentage=p / 100,
            ),
        )
        mediapipe_dnn_stream(
            1,
            10,
            Configs(
                render=False,
                test=[x for x in MODEL_TYPES],
                consecutive_frame_count=consecutive_frame_count,
                train_percentage=p / 100,
            ),
        )
    cv2.destroyAllWindows()


def cfc_1(input_type=INPUT_TYPES.Proc, cmp_size=2):
    cv2.startWindowThread()
    train_buffer = get_urfall_buffer(11, 20, Configs(input_type=input_type)) + get_florence_buffer(
        16, 20000000000, Configs(input_type=input_type)
    )
    test_buffer = get_urfall_buffer(1, 10, Configs(input_type=input_type)) + get_florence_buffer(
        1, 16, Configs(input_type=input_type)
    )
    for cfc in range(2, 21):
        cfg = Configs(
            render=False,
            input_type=input_type,
            consecutive_frame_count=cfc,
            compress_frames=cmp_size,
            train=[x for x in MODEL_TYPES],
            test=[],
            train_percentage=1.0,
        )
        mediapipe_dnn_stream(train_buffer, cfg)
        cfg_1 = Configs(
            render=False,
            input_type=input_type,
            consecutive_frame_count=cfc,
            compress_frames=cmp_size,
            train=[],
            test=[x for x in MODEL_TYPES],
            train_percentage=1.0,
        )
        mediapipe_dnn_stream(test_buffer, cfg_1)
    cv2.destroyAllWindows()


def man_test():
    cv2.startWindowThread()
    cfc = 2
    for threshold in range(10, 201, 10):
        mediapipe_dnn_stream(
            11,
            20,
            Configs(
                render=False,
                train=[x for x in MODEL_TYPES],
                consecutive_frame_count=cfc,
                train_percentage=1.0,
            ),
            threshold / 100,
        )
        mediapipe_dnn_stream(
            1,
            10,
            Configs(
                render=False,
                test=[x for x in MODEL_TYPES],
                consecutive_frame_count=cfc,
                train_percentage=1.0,
            ),
            threshold / 100,
        )
    cv2.destroyAllWindows()


def visualize_clf():
    cv2.startWindowThread()
    clf = DecisionTree(Configs(consecutive_frame_count=2), loadfromfile=True)
    clf.visualize()
    print(clf.to_text())
    cv2.destroyAllWindows()


if __name__ == "__main__":
    for cmp_size in range(0, 4):
        cfc_1(input_type=INPUT_TYPES.Relcom, cmp_size=cmp_size)