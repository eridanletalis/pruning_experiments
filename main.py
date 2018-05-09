import datetime
import glob
import os
import shutil

import tensorflow as tf

from bonesaw.network_restoration import get_restore_network_function
from bonesaw.weights_stripping import repack_graph, eval_weights_from_graph
from network_under_surgery.network_creation import get_layers_names_for_dataset, \
    get_create_network_function
from network_under_surgery.training_ops import create_training_ops, simple_train
from network_under_surgery.data_reading import load_dataset_to_memory
from result_show import show_results_against_compression

Flags = tf.app.flags
Flags.DEFINE_string('output_dir', None, 'The output directory of the checkpoint')
Flags.DEFINE_string('log_dir', None, 'Summary directory for tensorboard log')
Flags.DEFINE_string('source_model_name', None, 'Model name to search in output_dir, will train from scratch if None')
Flags.DEFINE_string('pruned_model_name', "pruned_network", 'Name for saved pruned network')

Flags.DEFINE_float('learning_rate', 0.001, 'The learning rate for the network')
Flags.DEFINE_float('beta1', 0.975, 'beta1 of Adam optimizer')
Flags.DEFINE_integer('batch_size', 32, 'Batch size of the input batch')
Flags.DEFINE_float('decay', 1e-6, 'Gamma of decaying')
Flags.DEFINE_integer('epochs', 20, 'The max epoch for the training')

Flags.DEFINE_string('task', "eval_repack_randomdrop", 'What we gonna do')
Flags.DEFINE_string('dataset', "cifar_10", 'What to feed to network')

FLAGS = Flags.FLAGS

random_drop_order = [0.1, 0.15, 0.2, 0.25, 0.3, 0.35]
random_drop_tries = 3

# Preparing directory, checking passed arguments
if FLAGS.output_dir is None:
    FLAGS.output_dir = "output_dir"
    if not os.path.exists(FLAGS.output_dir):
        os.mkdir(FLAGS.output_dir)

dataset = load_dataset_to_memory(FLAGS.dataset)

if FLAGS.log_dir is None:
    if not os.path.exists(os.path.join(FLAGS.output_dir, "log")):
        os.mkdir(os.path.join(FLAGS.output_dir, "log"))
    run_idx = 0
    while os.path.exists(os.path.join(FLAGS.output_dir, "log", str(run_idx))):
        run_idx += 1
    os.mkdir(os.path.join(FLAGS.output_dir, "log", str(run_idx)))
    FLAGS.log_dir = os.path.join(FLAGS.output_dir, "log", str(run_idx))

print("Loaded data from {}:\n\t{} train examples\n\t{} test examples\n\t{} classes\n\tinput shape: {}\n".format(
    dataset.dataset_label, dataset.train_images_num, dataset.test_images_num, dataset.classes_num, dataset.image_shape))


def create_network_under_surgery(sess, repacked_weights=None, layers_order=None):
    network_input = tf.placeholder(tf.float32, [None] + list(dataset.image_shape), 'main_input')
    network_target = tf.placeholder(tf.int32, [None, dataset.classes_num], 'main_target')
    begin_ts = datetime.datetime.now()
    if repacked_weights is not None and layers_order is not None:
        restore_network_fn = get_restore_network_function(dataset.dataset_label)
        network_logits = restore_network_fn(network_input, layers_order, repacked_weights, debug=False)
    else:
        create_network_fn = get_create_network_function(dataset.dataset_label)
        network_logits = create_network_fn(network_input, dataset.classes_num)
    print("Network created ({}), preparing ops".format(datetime.datetime.now() - begin_ts))
    network = create_training_ops(network_input, network_logits, network_target, FLAGS)
    train_writer = tf.summary.FileWriter(FLAGS.log_dir, sess.graph)
    saver = tf.train.Saver()
    sess.run(tf.global_variables_initializer())
    return network_input, network_target, network_logits, network, saver, train_writer


model_folder = dataset.dataset_label + "_model_bak"

if FLAGS.task == "train":

    with tf.Session() as sess:
        network_input, network_target, network_logits, network, saver, train_writer = \
            create_network_under_surgery(sess)
        print("Begin training")
        simple_train(sess, saver, train_writer, network, dataset, FLAGS)
        print("Training is over, moving model to separate folder")
        try:
            if not os.path.exists(model_folder):
                os.mkdir(model_folder)
            shutil.copy2(os.path.join(FLAGS.output_dir, "checkpoint"), os.path.join(model_folder, "checkpoint"))
            for filename in glob.glob(os.path.join(FLAGS.output_dir, "model_*")):
                shutil.copy2(filename, os.path.join(model_folder, os.path.split(filename)[-1]))
        except Exception as e:
            print("Could not relocate trained model: {}", str(e))

elif FLAGS.task in ["eval", "eval_repack", "eval_repack_randomdrop"]:
    repacked_weights_list, compressions = None, []
    with tf.Session() as sess:
        network_input, network_target, network_logits, network, saver, train_writer = \
            create_network_under_surgery(sess)

        ckpt = tf.train.get_checkpoint_state(model_folder)
        saver.restore(sess, ckpt.model_checkpoint_path)

        network_input = sess.graph.get_tensor_by_name("main_input:0")
        network_target = sess.graph.get_tensor_by_name("main_target:0")
        network_logits = sess.graph.get_tensor_by_name(
            get_layers_names_for_dataset(dataset.dataset_label)[-1] + "/BiasAdd:0")

        network = create_training_ops(network_input, network_logits, network_target, FLAGS)

        if FLAGS.task in ["eval", "eval_repack"]:
            loss, accuracy = sess.run([network.loss, network.accuracy_op], feed_dict={
                network.input_plh: dataset.test_images,
                network.target_plh: dataset.test_labels
            })
            print("Val loss after loading: {}".format(loss))
            print("Val accuracy after loading: {}".format(accuracy))

        if FLAGS.task != "eval_repack_randomdrop":
            random_drop_order = [0.0]

        if FLAGS.task == "eval":
            exit(0)

        repacked_weights_list = []
        for random_drop_p in random_drop_order:
            for random_drop_try in range(random_drop_tries):
                print("Repacking with {} random drop".format(random_drop_p))
                evaluated_trainable_variables, compression = repack_graph(
                    sess.graph, get_layers_names_for_dataset(dataset.dataset_label), random_drop=random_drop_p, debug=False)
                repacked_weights_list.append(evaluated_trainable_variables)
                compressions.append(compression)

    if FLAGS.task in ["eval_repack", "eval_repack_randomdrop"]:
        assert repacked_weights_list
        losses, accuracies = [], []
        for i, repacked_weights in enumerate(repacked_weights_list):
            print("{}/{}".format(i+1, len(repacked_weights_list)))
            with tf.Session() as sess:
                print("Restoring network with stripped weights...")
                network_input, network_target, network_logits, network, saver, train_writer = \
                    create_network_under_surgery(
                        sess, repacked_weights, get_layers_names_for_dataset(dataset.dataset_label))

                print("Running...")
                loss, accuracy = sess.run([network.loss, network.accuracy_op], feed_dict={
                    network.input_plh: dataset.test_images,
                    network.target_plh: dataset.test_labels
                })
                print("Val loss after repacking: {}".format(loss))
                print("Val accuracy after repacking: {}".format(accuracy))
                losses.append(loss)
                accuracies.append(accuracy)
        print(compressions)
        print(accuracies)
        print(losses)
        show_results_against_compression(compressions, accuracies, losses)

else:
    raise ValueError("Unknown task: " + FLAGS.task)

print("Done")
