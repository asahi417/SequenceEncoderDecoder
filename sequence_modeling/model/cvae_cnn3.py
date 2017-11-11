import tensorflow as tf
from tensorflow.contrib.layers import xavier_initializer_conv2d, variance_scaling_initializer, xavier_initializer
import tensorflow.contrib.slim as slim
import numpy as np


def image_size(_shape, stride):
    return int(np.ceil(_shape[0] / stride[0])), int(np.ceil(_shape[1] / stride[1]))


def convolution(x, weight_shape, stride, initializer, padding="SAME"):
    """ 2d convolution layer
    - weight_shape: width, height, input channel, output channel
    """
    weight = tf.Variable(initializer(shape=weight_shape))
    bias = tf.Variable(tf.zeros([weight_shape[-1]]), dtype=tf.float32)
    return tf.add(tf.nn.conv2d(x, weight, strides=[1, stride[0], stride[1], 1], padding=padding), bias)


def deconvolution(x, weight_shape, output_shape, stride, initializer, padding="SAME"):
    """ 2d deconvolution layer
    - weight_shape: width, height, input channel, output channel
    """
    weight = tf.Variable(initializer(shape=weight_shape))
    bias = tf.Variable(tf.zeros([weight_shape[2]]), dtype=tf.float32)
    _layer = tf.nn.conv2d_transpose(x, weight, output_shape=output_shape, strides=[1, stride[0], stride[1], 1],
                                    padding=padding, data_format="NHWC")
    return tf.add(_layer, bias)


def full_connected(x, weight_shape, initializer):
    """ fully connected layer
    - weight_shape: input size, output size
    """
    weight = tf.Variable(initializer(shape=weight_shape))
    bias = tf.Variable(tf.zeros([weight_shape[-1]]), dtype=tf.float32)
    return tf.add(tf.matmul(x, weight), bias)


def reconstruction_loss(original, reconstruction, eps=1e-10):
    """
    The reconstruction loss (the negative log probability of the input under the reconstructed Bernoulli distribution
    induced by the decoder in the data space). This can be interpreted as the number of "nats" required for
    reconstructing the input when the activation in latent is given.
    Adding 1e-10 to avoid evaluation of log(0.0)
    """
    _tmp = original * tf.log(eps + reconstruction) + (1 - original) * tf.log(eps + 1 - reconstruction)
    return -tf.reduce_sum(_tmp, 1)


def latent_loss(latent_mean, latent_log_sigma_sq):
    """
    The latent loss, which is defined as the Kullback Leibler divergence between the distribution in latent space
    induced by the encoder on the data and some prior. This acts as a kind of regularizer. This can be interpreted as
    the number of "nats" required for transmitting the the latent space distribution given the prior.
    """
    latent_log_sigma_sq = tf.clip_by_value(latent_log_sigma_sq, clip_value_min=-1e-10, clip_value_max=1e+2)
    return -0.5 * tf.reduce_sum(1 + latent_log_sigma_sq - tf.square(latent_mean) - tf.exp(latent_log_sigma_sq), 1)


class ConditionalVAE(object):
    """ Conditional VAE

    To trainable variables at FC be small and those at CNN be large

    - Encoder: input (3d matrix) -> CNN x 3 + FC x 1 -> latent
        CNN1 (ks: [6, 6], st: [3, 3], ch: 16),
        CNN2 (ks: [4, 4], st: [3, 3], ch: 32),
        CNN3 (ks: [2, 2], st: [2, 2], ch: 64)
    - Decoder: latent -> FC x 1 + DeCNN x 3 -> output (3d matrix)
        DeCNN1 (ks: [2, 2], st: [2, 2], ch: 32),
        DeCNN2 (ks: [3, 3], st: [3, 3], ch: 16),
        DeCNN3 (ks: [4, 4], st: [4, 4], ch: 1)
    """

    def __init__(self, network_architecture, activation=tf.nn.relu, learning_rate=0.001,
                 save_path=None, load_model=None, max_grad_norm=None):
        """
        :param dict network_architecture: dictionary with following elements
            n_input: shape of input
            n_z: dimensionality of latent space
            label_size: unique number of label
            batch_size: size of mini-batch
        :param activation: activation function (tensor flow function)
        :param float learning_rate:
        :param str save_path: path to save
        :param str load_model: load saved model
        """
        self.network_architecture = network_architecture
        self.activation = activation
        self.learning_rate = learning_rate
        self.max_grad_norm = max_grad_norm

        # Initializer
        if "relu" in self.activation.__name__:
            self.ini_c, self.ini = variance_scaling_initializer(), variance_scaling_initializer()
        else:
            self.ini_c, self.ini = xavier_initializer_conv2d(), xavier_initializer()

        # Create network
        self._create_network()

        # Summary
        tf.summary.scalar("loss", self.loss)
        # Launch the session
        self.sess = tf.Session(config=tf.ConfigProto(log_device_placement=False))
        # Summary writer for tensor board
        self.summary = tf.summary.merge_all()
        if save_path:
            self.writer = tf.summary.FileWriter(save_path, self.sess.graph)
        # Load model
        if load_model:
            tf.reset_default_graph()
            self.saver.restore(self.sess, load_model)

    def _create_network(self):
        """ Create Network, Define Loss Function and Optimizer """
        # tf Graph input
        __s = [self.network_architecture["batch_size"]] + self.network_architecture["n_input"]
        self.x = tf.placeholder(tf.float32, __s, name="input")
        __s = [self.network_architecture["batch_size"], self.network_architecture["label_size"]]
        self.y = tf.placeholder(tf.float32, __s, name="output")

        # Build conditional input
        _label = tf.reshape(self.y, [-1, 1, 1, self.network_architecture["label_size"]])
        _s = [self.network_architecture["batch_size"]] + self.network_architecture["n_input"][0:-1] + \
             [self.network_architecture["label_size"]]
        _one = tf.ones(_s)
        _label = _one * _label
        _layer = tf.concat([self.x, _label], axis=3)
        _ch = self.network_architecture["n_input"][2] + self.network_architecture["label_size"]

        # Encoder network to determine mean and (log) variance of Gaussian distribution in latent space
        with tf.variable_scope("encoder"):
            # convolution 1
            _out, _in, ks = 16, _ch, [6, 6]
            _layer = convolution(_layer, ks + [_in, _out], [3, 3], self.ini_c)
            _layer = self.activation(_layer)
            # convolution 2
            _out, _in, ks = 32, _out, [4, 4]
            _layer = convolution(_layer, ks + [_in, _out], [3, 3], self.ini_c)
            _layer = self.activation(_layer)
            # convolution 3
            _out, _in, ks = 64, _out, [2, 2]
            _layer = convolution(_layer, ks + [_in, _out], [2, 2], self.ini_c)
            _layer = self.activation(_layer)
            # full connect to get "mean" and "sigma"
            _layer = slim.flatten(_layer)
            _shape = _layer.shape.as_list()

            self.z_mean = full_connected(_layer, [_shape[-1], self.network_architecture["n_z"]], self.ini)
            self.z_log_sigma_sq = full_connected(_layer, [_shape[-1], self.network_architecture["n_z"]], self.ini)

        # Draw one sample z from Gaussian distribution
        eps = tf.random_normal((self.network_architecture["batch_size"], self.network_architecture["n_z"]),
                               mean=0, stddev=1, dtype=tf.float32)
        # z = mu + sigma*epsilon
        self.z = tf.add(self.z_mean, tf.multiply(tf.sqrt(tf.exp(self.z_log_sigma_sq)), eps))
        _layer = tf.concat([self.z, self.y], axis=1)

        # Decoder to determine mean of Bernoulli distribution of reconstructed input
        with tf.variable_scope("decoder"):
            stride_0, stride_1, stride_2 = [4, 4], [3, 3], [2, 2]
            _w0, _h0 = self.network_architecture["n_input"][0:-1]
            _w1, _h1 = image_size([_w0, _h0], stride_0)
            _w2, _h2 = image_size([_w1, _h1], stride_1)
            _w3, _h3 = image_size([_w2, _h2], stride_2)
            # full connect
            _in_size = self.network_architecture["n_z"] + self.network_architecture["label_size"]
            _out = 64
            _layer = full_connected(_layer, [_in_size, int(_w3 * _h3 * _out)], self.ini)
            _layer = self.activation(_layer)
            # reshape to the image
            _layer = tf.reshape(_layer, [-1, _w3, _h3, _out])
            # deconvolution 1
            _out, _in, ks = 32, _out, [2, 2]
            _layer = deconvolution(_layer, ks + [_out, _in], [self.network_architecture["batch_size"], _w2, _h2, _out],
                                   stride_2, self.ini_c)
            _layer = self.activation(_layer)
            # deconvolution 2
            _out, _in, ks = 16, _out, [3, 3]
            _layer = deconvolution(_layer, ks + [_out, _in], [self.network_architecture["batch_size"], _w1, _h1, _out],
                                   stride_1, self.ini_c)
            _layer = self.activation(_layer)
            # deconvolution 3
            _out, _in, ks = 1, _out, [4, 4]
            _logit = deconvolution(_layer, ks + [_out, _in], [self.network_architecture["batch_size"], _w0, _h0, _out],
                                   stride_0, self.ini_c)
            # -print(_in * _out * ks[0] * ks[1])
            self.x_decoder_mean = tf.nn.sigmoid(_logit)

        # Define loss function
        with tf.name_scope('loss'):
            self.re_loss = tf.reduce_mean(reconstruction_loss(original=self.x, reconstruction=self.x_decoder_mean))
            self.latent_loss = tf.reduce_mean(latent_loss(self.z_mean, self.z_log_sigma_sq))
            self.loss = tf.where(tf.is_nan(self.re_loss), 0.0, self.re_loss) + \
                tf.where(tf.is_nan(self.latent_loss), 0.0, self.latent_loss)

        # Define optimizer
        optimizer = tf.train.AdamOptimizer(self.learning_rate)
        if self.max_grad_norm:
            _var = tf.trainable_variables()
            grads, _ = tf.clip_by_global_norm(tf.gradients(self.loss, _var), self.max_grad_norm)
            self.train = optimizer.apply_gradients(zip(grads, _var))
        else:
            self.train = optimizer.minimize(self.loss)

        # saver
        self.saver = tf.train.Saver()

    def reconstruct(self, inputs, label):
        """Reconstruct given data. """
        assert len(inputs) == self.network_architecture["batch_size"]
        assert len(label) == self.network_architecture["batch_size"]
        return self.sess.run(self.x_decoder_mean, feed_dict={self.x: inputs, self.y: label})

    def encode(self, inputs, label):
        """ Embed given data to latent vector. """
        return self.sess.run(self.z_mean, feed_dict={self.x: inputs, self.y: label})

    def decode(self, label, z=None, std=0.01, mu=0):
        """ Generate data by sampling from latent space.
        If z_mu is not None, data for this point in latent space is generated.
        Otherwise, z_mu is drawn from prior in latent space.
        """
        if z is None:
            z = mu + np.random.randn(self.network_architecture["batch_size"], self.network_architecture["n_z"]) * std
        return self.sess.run(self.x_decoder_mean, feed_dict={self.z: z, self.y: label})


if __name__ == '__main__':
    import os

    # Ignore warning message by tensor flow
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

    # ConditionalVAE(10, {
    #   "n_input": [
    #     28,
    #     28,
    #     1
    #   ],
    #   "n_z": 20})