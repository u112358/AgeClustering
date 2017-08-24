# MIT License
#
# Copyright (c) 2017 BingZhang Hu
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
from util.inception_resnet_v1 import *
from util.file_reader import *
import os
from datetime import datetime
import time
import numpy as np
from tensorflow.python.ops import data_flow_ops
from util.progress import *
from tensorflow.contrib.tensorboard.plugins import projector
import scipy.io as sio
from shutil import copyfile

class AgeClusterMachine():
    """
    set different hinge for different age gap
    """

    def __init__(self):
        # data directory
        self.data_dir = '/scratch/BingZhang/dataset/CACD2000_Cropped'
        self.data_info = '/scratch/BingZhang/dataset/CACD2000/celenew2.mat'

        # self.data_dir = '/home/bingzhang/Documents/Dataset/CACD/CACD2000'
        # self.data_info = '/home/bingzhang/Documents/Dataset/CACD/celenew2.mat'

        # validation data

        self.val_dir = '/scratch/BingZhang/lfw_250/'
        # self.val_dir = '/home/bingzhang/Documents/Dataset/lfw'
        self.val_list = './data/val_list.txt'
        self.val_size = 144

        # image size
        self.image_height = 250
        self.image_width = 250
        self.image_channel = 3

        #
        self.model = '/scratch/BingZhang/facenet4drfr/model/20170512-110547/model-20170512-110547.ckpt-250000'
        # self.model = '/home/bingzhang/Workspace/PycharmProjects/20170512-110547/model-20170512-110547.ckpt-250000'
        self.cwd = os.getcwd()
        self.prefix = os.path.join(self.cwd, 'log')
        self.log_dir = os.path.join(self.prefix, datetime.strftime(datetime.now(), '%Y%m%d-%H%M%S'))
        self.model_dir = os.path.join(self.prefix, 'model.ckpt')

        # net parameters
        self.step = 0
        self.learning_rate = 0.006
        self.batch_size = 30
        self.embedding_bits = 128
        self.max_epoch = 1000

        self.nof_sampled_age = 20
        self.nof_images_per_age = 45
        self.age_sampled_examples = self.nof_images_per_age * self.nof_sampled_age

        # age affinity matrix, add to summary to be monitored
        self.age_affinity = tf.placeholder(tf.float32, [None, self.age_sampled_examples, self.age_sampled_examples, 1],
                                           name='age_affinity')
        self.age_affinity_binarized = tf.placeholder(tf.float32,
                                                     [None, self.age_sampled_examples, self.age_sampled_examples, 1],
                                                     name='age_affinity_binarized')
        self.nof_selected_age_triplets = tf.placeholder(tf.int32, name='nof_triplet')
        self.val_embeddings_array = np.zeros(shape=(self.val_size, self.embedding_bits),dtype='float32')
        self.val_embeddings_placeholder = tf.placeholder(tf.float32,[self.val_size,self.embedding_bits],name='val_embeddings_placeholder')
        self.val_embeddings = tf.Variable(tf.zeros([self.val_size,self.embedding_bits]),name='val_embeddings')
        self.assign_op = self.val_embeddings.assign(self.val_embeddings_placeholder)
        ''' input pipeline '''
        # placeholders
        self.path_placeholder = tf.placeholder(tf.string, [None, 3], name='paths')
        self.label_placeholder = tf.placeholder(tf.int64, [None, 3], name='indices')
        self.batch_size_placeholder = tf.placeholder(tf.int32, name='batch_size')
        # input queue (FIFO queue)
        self.input_queue = data_flow_ops.FIFOQueue(capacity=1000000, dtypes=[tf.string, tf.int64], shapes=[(3,), (3,)])
        self.enqueue_op = self.input_queue.enqueue_many([self.path_placeholder, self.label_placeholder])

        # de-queue an element from input_queue
        nof_process_threads = 4
        images_and_labels = []
        for _ in range(nof_process_threads):
            file_paths, labels = self.input_queue.dequeue()
            images = []
            for file_path in tf.unstack(file_paths):
                file_content = tf.read_file(file_path)
                try:
                    image = tf.image.decode_png(file_content)
                except:
                    image = tf.image.decode_jpeg(file_content)
                image.set_shape((self.image_width, self.image_height, self.image_channel))
                images.append(tf.image.per_image_standardization(image))
            images_and_labels.append([images, labels])
        # generate batch
        self.image_batch, self.label_batch = tf.train.batch_join(images_and_labels,
                                                                 batch_size=self.batch_size_placeholder,
                                                                 enqueue_many=True,
                                                                 capacity=nof_process_threads * self.batch_size,
                                                                 shapes=[(self.image_width, self.image_height,
                                                                          self.image_channel), ()],
                                                                 allow_smaller_final_batch=True,
                                                                 shared_name=None)
        ''' end of input pipeline '''

        # ops and tensors in graph
        self.embeddings = self.net_forward(self.image_batch)
        self.loss = self.get_triplet_loss(self.embeddings, self.label_batch)
        self.summary_op, self.average_op = self.get_summary()
        with tf.control_dependencies([self.average_op]):
            self.opt = tf.train.AdamOptimizer(learning_rate=self.learning_rate).minimize(self.loss)

    def net_forward(self, image_batch):
        # convolution layers
        net, _ = inference(image_batch, keep_probability=1.0, bottleneck_layer_size=128, weight_decay=0.0,
                           phase_train=True, reuse=None)
        embeddings = slim.fully_connected(net, self.embedding_bits, activation_fn=None,
                                          weights_initializer=tf.truncated_normal_initializer(stddev=0.1),
                                          weights_regularizer=slim.l2_regularizer(0.0))
        embeddings = tf.nn.l2_normalize(embeddings, dim=1, epsilon=1e-12, name='embeddings')
        return embeddings

    def get_triplet_loss(self, embeddings, deltas):
        anchor = embeddings[0:self.batch_size:3][:]
        positive = embeddings[1:self.batch_size:3][:]
        negative = embeddings[2:self.batch_size:3][:]
        deltas_ = tf.div(tf.to_float(tf.abs(deltas[0:self.batch_size:3] - deltas[2:self.batch_size:3])), 80.0)

        pos_dist = tf.reduce_sum(tf.square(tf.subtract(anchor, positive)), 1)
        neg_dist = tf.reduce_sum(tf.square(tf.subtract(anchor, negative)), 1)
        basic_loss = tf.add(tf.subtract(pos_dist, neg_dist), tf.reshape(deltas_, (self.batch_size / 3,)))
        loss = tf.reduce_mean(tf.maximum(basic_loss, 0.0), 0)
        return loss

    def get_triplet_loss_facenet(self, anchor, positive, negative, delta):
        pos_dist = tf.reduce_sum(tf.square(tf.subtract(anchor, positive)), 1)
        neg_dist = tf.reduce_sum(tf.square(tf.subtract(anchor, negative)), 1)

        basic_loss = tf.add(tf.subtract(pos_dist, neg_dist), delta)
        loss = tf.reduce_mean(tf.maximum(basic_loss, 0.0), 0)
        return loss

    def get_summary(self):
        with tf.name_scope('affinity'):
            tf.summary.image('original', self.age_affinity)
            tf.summary.image('binarized', self.age_affinity_binarized)
        with tf.name_scope('loss'):
            tf.summary.scalar('original_loss', self.loss)
            average = tf.train.ExponentialMovingAverage(0.9)
            average_op = average.apply([self.loss])
            tf.summary.scalar('averaged_loss', average.average(self.loss))
        with tf.name_scope('nof_triplets'):
            tf.summary.scalar('nof_triplets', self.nof_selected_age_triplets)

        return tf.summary.merge_all(), average_op

    # def test(self):
    #     with tf.Session() as sess:
    #         embeddings = tf.get_variable(name='fake_embeddings', shape=[6.0, 2.0], dtype=tf.float32,
    #                                      initializer=tf.truncated_normal_initializer(stddev=1))
    #         # delta = tf.Variable(name='fake_delta', initial_value=[[1.0],
    #         #                                                       [2.0]], dtype=tf.float32)
    #         sess.run(tf.global_variables_initializer())
    #         self.batch_size = 6
    #         delta = [1.0, 2.0]
    #         print sess.run(embeddings)
    #         print sess.run(self.get_triplet_loss(embeddings, delta),
    #                        feed_dict={self.delta_placeholder: np.reshape(delta, (1, -1))})

    def train(self):
        sess = tf.Session()
        sess.run(tf.global_variables_initializer())
        coord = tf.train.Coordinator()
        tf.train.start_queue_runners(coord=coord, sess=sess)
        summary_writer = tf.summary.FileWriter(self.log_dir, sess.graph)
        copyfile('./data/face.png',os.path.join(self.log_dir,'face.png'))
        copyfile('./data/label.tsv',os.path.join(self.log_dir,'label.tsv'))
        cacd = FileReader(self.data_dir, self.data_info, reproducible=True, contain_val=True, val_data_dir=self.val_dir,
                          val_list=self.val_list)
        # add an embedding to tensorboard
        config = tf.contrib.tensorboard.plugins.projector.ProjectorConfig()
        embedding_config = config.embeddings.add()
        embedding_config.tensor_name = self.val_embeddings.name
        embedding_config.sprite.image_path = os.path.join(self.log_dir, 'face.png')
        embedding_config.metadata_path = os.path.join(self.log_dir, 'label.tsv')
        # Specify the width and height of a single thumbnail.
        embedding_config.sprite.single_image_dim.extend([64, 64])
        tf.contrib.tensorboard.plugins.projector.visualize_embeddings(summary_writer, config)

        var = tf.trainable_variables()
        var = [v for v in var if str(v.name).__contains__('Inception')]
        saver = tf.train.Saver(var)
        saver.restore(sess, self.model)
        emb_saver = tf.train.Saver([self.val_embeddings])
        saved_time = 0


        for triplet_selection in range(self.max_epoch):

            if triplet_selection % 5 == 0 or triplet_selection < 5:
                val_paths = cacd.get_val(cacd.val_size)
                val_path_array = np.reshape(val_paths, (-1, 3))
                val_label_array = np.reshape(np.arange(cacd.val_size), (-1, 3))

                # FIFO enqueue
                sess.run(self.enqueue_op,
                         feed_dict={self.path_placeholder: val_path_array,
                                    self.label_placeholder: val_label_array})

                # forward propagation to get val embeddings
                print('Forward propagation on validation set')
                nof_batches = int(np.ceil(cacd.val_size / self.batch_size))
                for i in range(nof_batches):
                    batch_size = min(cacd.val_size - i * self.batch_size, self.batch_size)
                    emb, label = sess.run([self.embeddings, self.label_batch],
                                          feed_dict={self.batch_size_placeholder: batch_size})
                    self.val_embeddings_array[label, :] = emb

            # select examples to forward propagation
            paths, labels = cacd.select_age_path(self.nof_sampled_age, self.nof_images_per_age)
            nof_examples = len(paths)
            path_array = np.reshape(paths, (-1, 3))
            index_array = np.reshape(np.arange(nof_examples), (-1, 3))
            embedding_array = np.zeros(shape=(nof_examples, self.embedding_bits))
            label_index = []

            # FIFO enqueue
            sess.run(self.enqueue_op,
                     feed_dict={self.path_placeholder: path_array, self.label_placeholder: index_array})

            # forward propagation to get current embeddings
            nof_batches = int(np.ceil(nof_examples / self.batch_size))
            for batch_index in range(nof_batches):
                batch_size = min(nof_examples - batch_index * self.batch_size, self.batch_size)
                emb, index = sess.run([self.embeddings, self.label_batch],
                                      feed_dict={self.batch_size_placeholder: batch_size})
                embedding_array[index, :] = emb
                label_index.append(index)
            # labels = labels[np.reshape(label_index,(-1,1))]

            # compute affinity matrix on batch
            aff = []
            for idx in range(nof_examples):
                aff.append(np.sum(np.square(embedding_array[idx][:] - embedding_array), 1))
            aff_binarized = binarize_affinity(aff, self.nof_images_per_age)

            triplets = select_triplets_by_label(embedding_array, self.nof_sampled_age, self.nof_images_per_age, labels)
            triplet_path_array = paths[triplets][:]
            triplet_label_array = labels[triplets][:]
            nof_triplets = len(triplet_path_array)
            print("%d triplets selected" % nof_triplets)

            # FIFO enqueue
            sess.run(self.enqueue_op,
                     feed_dict={self.path_placeholder: triplet_path_array, self.label_placeholder: triplet_label_array})
            # train on selected triplets
            nof_batches = int(np.ceil(nof_triplets * 3 / self.batch_size))
            for i in range(nof_batches):
                batch_size = min(nof_triplets * 3 - i * self.batch_size, self.batch_size)
                summary, label_, loss, _ = sess.run(
                    [self.summary_op, self.label_batch, self.loss, self.opt],
                    feed_dict={self.batch_size_placeholder: batch_size,
                               self.age_affinity: np.reshape(aff, [1, self.age_sampled_examples,
                                                                   self.age_sampled_examples,
                                                                   1]),
                               self.age_affinity_binarized: np.reshape(aff_binarized, [1,
                                                                                       self.age_sampled_examples,
                                                                                       self.age_sampled_examples,
                                                                                       1]),
                               self.nof_selected_age_triplets: nof_triplets})
                # write in summary
                summary_writer.add_summary(summary, self.step)
                progress(i + 1, nof_batches, str(triplet_selection) + 'th Epoch',
                         'Batches loss:' + str(loss))  # a command progress bar to watch training progress
                self.step += 1

                # save model
                if self.step % 20000 == 0 :
                    saver.save(sess, self.model_dir, global_step=self.step)
                if self.step %1000==0 or saved_time<5:
                    print self.val_embeddings_array
                    print '\n\n----------------------------\n\n'
                    print sess.run(self.assign_op,feed_dict={self.val_embeddings_placeholder:self.val_embeddings_array})
                    emb_saver.save(sess,os.path.join(self.prefix,'model_emb.ckpt'),global_step=self.step)
                    saved_time+=1


def select_triplets_by_label(embeddings, nof_attr, nof_images_per_attr, labels):
    triplet = []
    for anchor_id in range(nof_attr * nof_images_per_attr):
        dist = np.sum(np.square(embeddings - embeddings[anchor_id]), 1)
        for pos_id in range(anchor_id + 1, (anchor_id // nof_images_per_attr + 1) * nof_images_per_attr):
            neg_dist = np.copy(dist)
            neg_dist[(anchor_id // nof_images_per_attr) * nof_images_per_attr:(
                                                                                  anchor_id // nof_images_per_attr + 1) * nof_images_per_attr] = np.NAN
            deltas = (labels - labels[anchor_id]) / 80.0
            neg_ids = np.where(neg_dist - dist[pos_id] < np.abs(deltas))[0]
            nof_neg_ids = len(neg_ids)
            if nof_neg_ids > 10:
                # rand_id = np.random.randint(nof_neg_ids)
                # neg_id = neg_ids[rand_id]
                neg_id = np.argsort(neg_dist)[0:10]
                neg_id = random.sample(neg_id, 1)[0]
                triplet.append([anchor_id, pos_id, neg_id])
    np.random.shuffle(triplet)
    return triplet


def binarize_affinity(aff, k):
    temp = np.argsort(aff)
    ranks = np.arange(len(aff))[np.argsort(temp)]
    ranks[np.where(ranks > k)] = 255
    return ranks


if __name__ == '__main__':
    instance = AgeClusterMachine()
    instance.train()
