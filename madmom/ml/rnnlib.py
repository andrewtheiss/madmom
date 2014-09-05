#!/usr/bin/env python
# encoding: utf-8
"""
This file contains all functionality needed for interaction with RNNLIB.

You need a working RNNLIB which can be obtained here:
http://sourceforge.net/apps/mediawiki/rnnl/index.php?title=Main_Page
Please build the software and put the resulting binary somewhere in your $PATH
or set the binary location in the RNNLIB variable below.

Note: RNNLIB is rather slow, madmom.ml.rnn serves as a (faster) purely Python
      based replacement for testing previously trained neural networks. The
      network configurations can be converted for testing with madmom.ml.rnn by
      RnnConfig('trained_network.save').save_model('converted_file').

@author: Sebastian Böck <sebastian.boeck@jku.at>

"""

import numpy as np

import os.path
import re
import shutil
import tempfile

from Queue import Queue
from threading import Thread
import multiprocessing
import subprocess

from ..features import Activations

# rnnlib binary, please see comment above
RNNLIB = 'rnnlib'


# TODO: inherit from features.Activations
#       add another @classmethod constructor or overwrite __new__()?
class RnnlibActivations(np.ndarray):
    """
    Class for reading in activations as written by RNNLIB.

    """
    def __new__(cls, filename, fps=None, labels=None):
        # default is only one label
        labels = [1]
        label = 0
        # read in the file
        with open(filename, 'r') as f:
            activations = None
            for line in f:
                # read in the header
                if line.startswith('#'):
                    continue
                if line.startswith('LABEL'):
                    labels = line.split(": ", 1)[1].split()
                    continue
                if line.startswith('DIMENSION'):
                    dimensions = int(line.split(": ", 1)[1])
                    # init the matrix
                    activations = np.zeros((dimensions, len(labels)))
                    continue
                # make sure we have an activations array
                if activations is None:
                    raise AssertionError('no activations initialised')
                # read in the data
                if labels:
                    activations[:, label] = np.fromstring(line, sep=' ')
                    # increase counter
                    label += 1
                else:
                    activations = np.fromstring(line, sep=' ')
        # close the file
        f.close()
        # cast to RnnlibActivations
        obj = np.asarray(activations.astype(np.float32)).view(cls)
        # set attributes
        obj._labels = labels
        obj._fps = fps
        # return the object
        return obj

    def __array_finalize__(self, obj):
        if obj is None:
            return
        # set default values here
        self._labels = getattr(obj, '_labels', None)
        self._fps = getattr(obj, '_fps', None)

    @property
    def labels(self):
        """Labels for classes."""
        return self._labels

    @property
    def fps(self):
        """Frames per second."""
        return self._fps


# helper functions for .nc file creation
def max_len(strings):
    """
    Determine the maximum length of an array of the given strings.
    :param strings: list with strings
    :return:        maximum length of these strings

    """
    return len(max(strings, key=len))


def expand_and_terminate(strings):
    """
    Expand and null-terminate the given strings to a common length.

    :param strings: a list of strings
    :return:        expanded and null-terminated list of strings

    """
    # each string must have the same length and must be null-terminated
    terminated_strings = []
    max_length = max_len(strings) + 1
    for string in strings:
        # expand each string to the maximum length with \0's
        string += '\0' * (max_length - len(string))
        terminated_strings.append(string)
    return terminated_strings


# FIXME: if we inherit from scipy.io.netcdf.NetCDFFile and omit the self.nc
# stuff and try to extend the class with properties directly, the setters do
# not work! why?
# noinspection PyPep8Naming
class NetCDF(object):
    """
    NetCDF Class is a simple NetCDFFile wrapper with some extensions for use
    with RNNLIB.

    """
    def __init__(self, filename, mode):
        """
        Creates a new NetCDF object.

        :param filename: open a .nc-file with the given filename
        :param mode:     {'r', 'w'} open an existing file for reading / writing

        Note: The scipy module has a bug which let's you alter variables and
              dimensions if the file is opened in 'r' mode, but does not throw
              an error if the flush() or close() methods are called and does
              not save the altered values!

        """
        from scipy.io.netcdf import NetCDFFile
        # open the file
        self.nc = NetCDFFile(filename, mode)

    def close(self):
        """Closes the file."""
        self.nc.close()

    def flush(self):
        """Flushes the file to disk."""
        self.nc.flush()

    @property
    def filename(self):
        """Name of the file."""
        return self.nc.filename

    @property
    def num_sequences(self):
        """Number of sequences."""
        # mandatory
        try:
            return self.nc.dimensions['numSeqs']
        except KeyError:
            return 0

    @num_sequences.setter
    def num_sequences(self, num_sequences):
        """
        Set the number of sequences.

        :param num_sequences: number of sequences [int]

        """
        try:
            self.nc.createDimension('numSeqs', num_sequences)
        except IOError:
            self.nc.dimensions['numSeqs'] = num_sequences

    @property
    def num_dimensions(self):
        """Number of dimensions of the sequences."""
        # mandatory
        try:
            return self.nc.dimensions['numDims']
        except KeyError:
            return 0

    @num_dimensions.setter
    def num_dimensions(self, num_dimensions):
        """
        Set the number of dimensions of the sequences.

        :param num_dimensions: number of dimensions of the sequences [int]

        """
        try:
            self.nc.createDimension('numDims', num_dimensions)
        except IOError:
            self.nc.dimensions['numDims'] = num_dimensions

    @property
    def num_time_steps(self):
        """Total number of time steps (of all sequences)."""
        # mandatory
        try:
            return self.nc.dimensions['numTimesteps']
        except KeyError:
            return 0

    @num_time_steps.setter
    def num_time_steps(self, num_time_steps):
        """
        Set the total number of time steps (of all sequences).

        :param num_time_steps: number of time steps [int]

        """
        try:
            self.nc.createDimension('numTimesteps', num_time_steps)
        except IOError:
            self.nc.dimensions['numTimesteps'] = num_time_steps

    @property
    def input_pattern_size(self):
        """Size of the input patterns."""
        # mandatory
        try:
            return self.nc.dimensions['inputPattSize']
        except KeyError:
            return None

    @input_pattern_size.setter
    def input_pattern_size(self, input_pattern_size):
        """
        Set the size of the input patterns.

        :param input_pattern_size: input pattern size [int]

        """
        try:
            self.nc.createDimension('inputPattSize', input_pattern_size)
        except IOError:
            self.nc.dimensions['inputPattSize'] = input_pattern_size

    # dimensions needed for indicated tasks
    # TODO: use decorators to check the presence of these depending on the task

    @property
    def num_labels(self):
        """Number of different labels."""
        # classification, sequence_classification, transcription
        try:
            return self.nc.dimensions['numLabels']
        except KeyError:
            return None

    @num_labels.setter
    def num_labels(self, num_labels):
        """
        Set the number of different labels (classification, sequence
        classification, transcription).

        :param num_labels: number of labels [int]

        """
        try:
            self.nc.createDimension('numLabels', num_labels)
        except IOError:
            self.nc.dimensions['numLabels'] = num_labels

    @property
    def max_label_length(self):
        """Maximum length of the labels."""
        # classification, sequence_classification, transcription
        try:
            return self.nc.dimensions['maxLabelLength']
        except KeyError:
            return None

    @max_label_length.setter
    def max_label_length(self, max_label_length):
        """
        Set the maximum label length (classification, sequence classification,
        transcription).

        :param max_label_length: maximum label length [int]

        """
        try:
            self.nc.createDimension('maxLabelLength', max_label_length)
        except IOError:
            self.nc.dimensions['maxLabelLength'] = max_label_length

    @property
    def target_pattern_size(self):
        """Size of the target pattern vector."""
        # regression
        try:
            return self.nc.dimensions['targetPattSize']
        except KeyError:
            return None

    @target_pattern_size.setter
    def target_pattern_size(self, target_pattern_size):
        """
        Set the size of the target pattern vector (regression).

        :param target_pattern_size: target pattern vector size

        """
        try:
            self.nc.createDimension('targetPattSize', target_pattern_size)
        except IOError:
            self.nc.dimensions['targetPattSize'] = target_pattern_size

    @property
    def max_target_string_length(self):
        """Maximum length of the target strings."""
        # sequence_classification, transcription
        try:
            return self.nc.dimensions['maxTargStringLength']
        except KeyError:
            return None

    @max_target_string_length.setter
    def max_target_string_length(self, max_target_string_length):
        """
        Set the maximum target strings length (sequence classification,
        transcription).

        :param max_target_string_length: maximum target strings length [int]

        """
        try:
            self.nc.createDimension('maxTargStringLength',
                                    max_target_string_length)
        except IOError:
            self.nc.dimensions['maxTargStringLength'] = \
                max_target_string_length

    @property
    def max_sequence_tag_length(self):
        """Maximum length of the sequence tags."""
        # optional
        try:
            return self.nc.dimensions['maxSeqTagLength']
        except KeyError:
            return None

    @max_sequence_tag_length.setter
    def max_sequence_tag_length(self, max_sequence_tag_length):
        """
        Set the maximum sequence tag length.

        :param max_sequence_tag_length: maximum sequence tag length [int]

        """
        try:
            self.nc.createDimension('maxSeqTagLength', max_sequence_tag_length)
        except IOError:
            self.nc.dimensions['maxSeqTagLength'] = max_sequence_tag_length

    # VARIABLES

    @property
    def inputs(self):
        """Input vectors."""
        # mandatory
        try:
            var = self.nc.variables['inputs']
            #return var.getValue()
            return var.data
        except KeyError:
            return None

    @inputs.setter
    def inputs(self, inputs):
        """
        Set the inputs.

        :param inputs: input vectors [float array]

        """
        inputs = np.atleast_2d(inputs)
        # set the seqDims if not already done
        if not self.sequence_dimensions:
            self.sequence_dimensions = [np.shape(inputs)[0]]
        if not self.num_time_steps:
            self.num_time_steps = np.shape(inputs)[0]
        if not self.input_pattern_size:
            self.input_pattern_size = np.shape(inputs)[1]
        var = self.nc.createVariable('inputs', 'f', ('numTimesteps',
                                                     'inputPattSize'))
        var[:] = inputs.astype(np.float32)

    @property
    def sequence_dimensions(self):
        """Sequence dimensions."""
        # mandatory
        try:
            var = self.nc.variables['seqDims']
            #return var.getValue()
            return var.data
        except KeyError:
            return None

    @sequence_dimensions.setter
    def sequence_dimensions(self, sequence_dimensions):
        """
        Set the sequence dimensions.

        :param sequence_dimensions: sequence dimensions [int array]

        """
        sequence_dimensions = np.atleast_2d(sequence_dimensions)
        if not self.num_sequences:
            self.num_sequences = np.shape(sequence_dimensions)[0]
        if not self.num_dimensions:
            self.num_dimensions = np.shape(sequence_dimensions)[1]
        var = self.nc.createVariable('seqDims', 'i', ('numSeqs', 'numDims'))
        var[:] = sequence_dimensions.astype(np.int32)

    # variables needed for indicated tasks
    # TODO: use decorators to check the presence of these depending on the task

    @property
    def target_classes(self):
        """Target classes."""
        # classification
        try:
            var = self.nc.variables['targetClasses']
            return var.data
        except KeyError:
            return None

    @target_classes.setter
    def target_classes(self, target_classes):
        """
        Set the target classes (classification).

        :param target_classes: target classes [array with class indices]

        """
        if not self.num_time_steps:
            self.num_time_steps = np.shape(target_classes)[0]
        if not self.labels:
            self.labels = np.unique(target_classes)
        var = self.nc.createVariable('targetClasses', 'i', ('numTimesteps',))
        var[:] = target_classes.astype(np.int64)

    @property
    def labels(self):
        """Labels."""
        # classification, sequence_classification, transcription
        try:
            var = self.nc.variables['labels']
            return var.data
        except KeyError:
            return None

    @labels.setter
    def labels(self, labels):
        """
        Set the labels (classification, sequence classification,
        transcription).

        :param labels: labels [list of strings]

        """
        # TODO: make a list if a single value is given?
        # convert the labels to a integer array
        labels = np.asarray(labels, np.int)
        # convert the labels to a strings array
        labels = np.asarray(labels, np.str)
        # set the number of labels
        if not self.num_labels:
            self.num_labels = np.shape(labels)[0]
        # set the maximum length of the labels
        if not self.max_label_length:
            # set the maximum length of the label names
            self.max_label_length = max_len(labels) + 1
        # all labels must be the same length and null-terminated
        labels = expand_and_terminate(labels)
        var = self.nc.createVariable('labels', 'c',
                                     ('numLabels', 'maxLabelLength'))
        var[:] = labels

    @property
    def target_patterns(self):
        """Target patterns."""
        # regression
        try:
            var = self.nc.variables['targetPatterns']
            return var.data
        except KeyError:
            return None

    @target_patterns.setter
    def target_patterns(self, target_patterns):
        """
        Set the target patterns (regression).

        :param target_patterns: target patterns [float array]

        """
        # TODO: make a list if a single value is given?
        if not self.num_time_steps:
            self.num_time_steps = np.shape(target_patterns)[0]
        if not self.target_pattern_size:
            self.target_pattern_size = np.shape(target_patterns)[1]
        var = self.nc.createVariable('targetPatterns', 'f',
                                     ('numTimesteps', 'targetPattSize'))
        var[:] = target_patterns.astype(np.float32)

    @property
    def target_strings(self):
        """Target strings."""
        # sequence_classification, transcription
        try:
            var = self.nc.variables['targetStrings']
            return var.data
        except KeyError:
            return None

    @target_strings.setter
    def target_strings(self, target_strings):
        """
        Set the target strings (sequence classification, transcription).

        :param target_strings: target strings [list of strings]

        """
        # TODO: make a list if a single value is given?
        if not self.num_sequences:
            self.num_sequences = len(target_strings)
        if not self.max_target_string_length:
            self.max_target_string_length = max_len(target_strings) + 1
        # all targetStrings must be the same length and null-terminated
        targetStrings = expand_and_terminate(target_strings)
        var = self.nc.createVariable('targetStrings', 'c',
                                     ('numTimesteps', 'maxTargStringLength'))
        var[:] = targetStrings

    @property
    def sequence_tags(self):
        """Sequence tags."""
        # optional
        try:
            var = self.nc.variables['seqTags']
            #return var.getValue()
            return var.data
        except KeyError:
            return None

    @sequence_tags.setter
    def sequence_tags(self, sequence_tags):
        """
        Set the sequence tags (optional data).

        :param sequence_tags: sequence tags [list of strings]
        """
        # make a list if a single value is given
        if isinstance(sequence_tags, str):
            sequence_tags = [sequence_tags]
        if not self.num_sequences:
            self.num_sequences = len(sequence_tags)
        if not self.max_sequence_tag_length:
            self.max_sequence_tag_length = max_len(sequence_tags) + 1
        # all seqTags must be the same length and null-terminated
        sequence_tags = expand_and_terminate(sequence_tags)
        var = self.nc.createVariable('seqTags', 'c', ('numSeqs',
                                                      'maxSeqTagLength'))
        var[:] = sequence_tags


# .nc file creation
def create_nc_file(filename, data, targets, tags=None):
    """
    Create a .nc file with the given input data and targets.

    :param filename: name of the file to create
    :param data:     input data
    :param targets:  corresponding targets
    :param tags:     additional information [optional]

    """
    # create the .nc file
    nc = NetCDF(filename, 'w')
    # input data handling
    if isinstance(data, np.ndarray):
        # data in correct format
        nc.inputs = data
    elif isinstance(data, list):
        # we need to stack the data
        inputs = None
        for d in data:
            if inputs is None:
                # use first as is
                inputs = d
            else:
                # stack all others
                inputs = np.hstack((inputs, d))
        # store them in .nc file
        nc.inputs = inputs
    else:
        raise TypeError("Invalid input data type.")
    # ground truth
    if targets.ndim == 1:
        nc.target_classes = targets
    else:
        nc.target_patterns = targets
    # tags
    if tags:
        nc.sequence_tags = str(tags)
    # save file
    nc.close()
    # return
    # TODO: return a tuple (fd + filename)?
    return filename


# .nc file testing
class TestThread(Thread):
    """
    Class for testing a .nc file against multiple networks and distributing the
    work to multiple threads.

    """
    def __init__(self, work_queue, return_queue, verbose=2):
        """
        Test a file against multiple neural networks.

        :param work_queue:   queue with work items
        :param return_queue: queue for the results
        :param verbose:      show RNNLIB's output

        """
        # init the thread
        super(TestThread, self).__init__()
        # set attributes
        self.work_queue = work_queue
        self.return_queue = return_queue
        self.verbose = verbose
        self.kill = False

    def run(self):
        """Test file against all neural networks in the queue."""
        while not self.kill:
            # grab the first work item from queue
            nc_file, nn_file = self.work_queue.get()
            # create a tmp directory for each thread
            tmp_work_path = tempfile.mkdtemp()
            # test the file against the network
            args = [RNNLIB,
                    '--verbose=true',
                    '--display=true',
                    '--autosave=false',
                    '--dumpPath=%s/' % tmp_work_path,
                    '--dataset=test',
                    '--dataFileNum=0',
                    '--sequence=0',
                    '--trainFile=""',
                    '--valFile=""',
                    '--testFile=%s' % nc_file, nn_file]
            try:
                if self.verbose > 1:
                    subprocess.call(args)
                else:
                    with open(os.devnull, 'w') as devnull:
                        subprocess.call(args, stdout=devnull, stderr=devnull)
            except OSError:
                # TODO: which exception should be raised?
                raise SystemExit('rnnlib binary not found')
            # read the activations
            act = None
            try:
                # classification output
                act = RnnlibActivations('%s/output_outputActivations' %
                                        tmp_work_path)
            except IOError:
                # could not read in the activations, try regression
                # TODO: make regression task work as well
                #       until then just output the log
                with open("%s/log" % tmp_work_path, 'rb') as log:
                    print log.read()
                raise RuntimeError("Error while RNNLIB processing.")
            finally:
                # put a tuple with nc file, nn file and activations
                # in the return queue
                self.return_queue.put((nc_file, nn_file, act))
                # clean up
                shutil.rmtree(tmp_work_path)
                # signal to queue that job is done
                self.work_queue.task_done()


def create_pool(threads=2, verbose=False):
    """
    Create a pool of working threads.

    :param threads:  number of parallel threads
    :param verbose:  be verbose
    :return:         a tuple with working and return queues

    Note: the work queue must contain tuples with (nc_file, nn_file),
          the return queue contains the same tuples extended by the activations
          (nc_file, nn_file, activations).

    """
    # a queue for the work items
    work_queue = Queue()
    # a queue for the results
    return_queue = Queue()
    # start N threads parallel
    workers = [TestThread(work_queue, return_queue, verbose)
               for _ in range(threads)]
    for w in workers:
        w.setDaemon(True)
        w.start()
    # return the queues
    return work_queue, return_queue


def test_nc_files(nc_files, nn_files, work_queue, return_queue):
    """
    Test a list of .nc files against multiple neural networks.

    :param nc_files:     list with .nc files to be tested
    :param nn_files:     list with network files
    :param work_queue:   a work queue
    :param return_queue: a return queue
    :return:             list with activations
                         (a numpy array for each .nc file)

    """
    if not nc_files:
        raise ValueError('no .nc files given')
    if not nn_files:
        raise ValueError('no pre-trained neural network files given')
    # put a combination of .nc files and neural networks in the queue
    for nc_file in nc_files:
        for nn_file in nn_files:
            work_queue.put((nc_file, nn_file))
    # wait until everything has been processed
    work_queue.join()
    # init return list
    activations = [None] * len(nc_files)
    num_activations = [1] * len(nc_files)
    # get all the activations and process them accordingly
    while not return_queue.empty():
        # get the tuple
        nc_file, nn_file, act = return_queue.get()
        # at which index should we put the activations in the return list
        nc_idx = nc_files.index(nc_file)
        # copy the activations to the returning list
        if activations[nc_idx] is None:
            # store the activations
            activations[nc_idx] = act
        else:
            # add the activations to the existing ones
            activations[nc_idx] += act
            # increase counter
            num_activations[nc_idx] += 1
    # average the activations
    for i in range(len(activations)):
        if num_activations[i] > 0:
            activations[i] /= num_activations[i]
    # return activations
    return activations


class RnnlibConfigFile(object):
    """Rnnlib config file class."""

    def __init__(self, filename=None):
        """
        Creates a new RNNLIB instance.

        :param filename: name of the config file for rnnlib

        """
        # container for weights
        self.w = {}
        # attributes
        self.train_files = None
        self.val_files = None
        self.test_files = None
        self.layer_sizes = None
        self.layer_types = None
        self.bidirectional = False
        self.task = None
        self.learn_rate = None
        self.momentum = None
        self.optimizer = None
        self.rand_seed = 0
        # read in file if a file name is given
        self.filename = filename
        if filename:
            self.load(filename)

    def load(self, filename):
        """
        Load the configuration from file.

        :param filename: name of the configuration file

        """
        # open the config file
        f = open(filename, 'r')
        # read in every line
        for line in f.readlines():
            # save the file sets
            if line.startswith('trainFile'):
                self.train_files = line[:].split()[1].split(',')
            elif line.startswith('valFile'):
                self.val_files = line[:].split()[1].split(',')
            elif line.startswith('testFile'):
                self.test_files = line[:].split()[1].split(',')
            # size and type of hidden layers
            elif line.startswith('hiddenSize'):
                self.layer_sizes = np.array(line[:].split()[1].split(','),
                                            dtype=np.int).tolist()
                # number of the output layer
                num_output_layer = len(self.layer_sizes)
            elif line.startswith('hiddenType'):
                hidden_type = line[:].split()[1]
                self.layer_types = [hidden_type] * len(self.layer_sizes)
            # task
            elif line.startswith('task'):
                self.task = line[:].split()[1]
            # save the weights
            elif line.startswith('weightContainer_'):
                # line format: weightContainer_bias_to_hidden_0_0_weights \
                # num_weights weight0 weight1 ...
                parts = line[:].split()
                # only use the weights
                if parts[0].endswith('_weights'):
                    # get rid of beginning and end
                    name = re.sub('weightContainer_', '', str(parts[0][:]))
                    name = re.sub('_weights', '', name)
                    # alter the name to a more useful schema
                    name = re.sub('_to_', '_', name)
                    name = re.sub('input_', 'i_', name)
                    name = re.sub('hidden_', 'layer_', name)
                    name = re.sub('bias_', 'b_', name)
                    name = re.sub('_output', '_o', name)
                    name = re.sub('gather_._', 'i_', name)
                    name = re.sub('_peepholes', '_peephole_weights', name)
                    # hidden layer handling
                    for i in range(len(self.layer_sizes)):
                        # recurrent connections
                        name = re.sub('layer_%s_0_layer_%s_0_delay.*' % (i, i),
                                      'layer_%s_0_recurrent_weights' % i, name)
                        name = re.sub('layer_%s_1_layer_%s_1_delay.*' % (i, i),
                                      'layer_%s_1_recurrent_weights' % i, name)
                    # set bidirectional mode
                    if '0_1' in name:
                        self.bidirectional = True
                    # start renaming / renumbering
                    if name.startswith('i_'):
                        # weights
                        name = "%s_weights" % name[2:]
                    if name.startswith('b_'):
                        # bias
                        name = "%s_bias" % name[2:]
                    if name.startswith('o_'):
                        # output layer
                        name = "layer_%s_0_%s" % (num_output_layer, name[2:])
                    if name.endswith('_o'):
                        name = re.sub('layer_%s_0_o' % (num_output_layer - 1),
                                      'layer_%s_0_weights' % num_output_layer,
                                      name)
                        name = re.sub('layer_%s_1_o' % (num_output_layer - 1),
                                      'layer_%s_1_weights' % num_output_layer,
                                      name)
                    # save the weights
                    self.w[name] = np.array(parts[2:], dtype=np.float32)
        # append output layer size
        output_size = self.w['layer_%s_0_bias' % num_output_layer].size
        self.layer_sizes.append(output_size)
        # set the output layer type
        if self.task == 'classification':
            self.layer_types.append('sigmoid')
        elif self.task == 'regression':
            self.layer_types.append('linear')
        else:
            raise ValueError('unknown task, cannot set type of output layer.')
        # stack the output weights
        if self.bidirectional:
            num_output = len(self.layer_sizes) - 1
            size_output = self.layer_sizes[num_output]
            bwd = self.w.pop('layer_%s_0_weights' % num_output)
            fwd = self.w.pop('layer_%s_1_weights' % num_output)
            # reshape weights
            bwd = bwd.reshape((size_output, -1))
            fwd = fwd.reshape((size_output, -1))
            # stack weights
            self.w['layer_%s_0_weights' % num_output] = np.hstack((bwd, fwd))
        # close the file
        f.close()

    def save(self, filename):
        """
        Save the RNNLIB config file.

        :param filename: name of the config file

        """
        # write the config file(s)
        # TODO: use madmom.utils.open
        f = open(filename, 'wb')
        f.write('task %s\n' % self.task)
        f.write('autosave true\n')
        # use the 1st hidden layer
        f.write('hiddenType %s\n' % self.layer_types[0])
        f.write('hiddenSize %s\n' % ",".join(str(x) for x in self.layer_sizes))
        f.write('bidirectional %s\n' % str(self.bidirectional).lower())
        f.write('dataFraction 1\n')
        f.write('maxTestsNoBest %s\n' % 20)
        f.write('learnRate %s\n' % str(self.learn_rate))
        f.write('momentum %s\n' % str(self.momentum))
        f.write('optimiser %s\n' % str(self.optimizer))
        f.write('randSeed %s\n' % str(self.rand_seed))
        if len(self.train_files) > 0:
            f.write('trainFile %s\n' % ",".join(self.train_files))
        if len(self.val_files) > 0:
            f.write('valFile %s\n' % ",".join(self.val_files))
        if len(self.test_files) > 0:
            f.write('testFile %s\n' % ",".join(self.test_files))
        f.close()

    def test(self, out_dir=None, file_set='test', threads=2, verbose=False):
        """
        Test the given set of files.

        :param out_dir:  output directory for activations
        :param file_set: which set should be tested {train, val, test}
        :param threads:  number of working threads
        :param verbose:  verbose output
        :return:         the output directory

        Note: If given, out_dir must exist. If none is given, an output
              directory is created.

        """
        # if no output directory was given, use the name of the file + set
        if out_dir is None:
            out_dir = "%s.%s" % (os.path.splitext(self.filename)[0], file_set)
        # create output directory
        try:
            os.mkdir(out_dir)
        except OSError:
            # directory exists already, update modification date
            os.utime(out_dir, None)
        # test all files of the given set
        nc_files = getattr(self, "%s_files" % file_set)
        # create a pool of workers
        work_queue, return_queue = create_pool(threads, verbose)
        # test all files
        activations = test_nc_files(nc_files, [self.filename], work_queue,
                                    return_queue)
        # save all activations
        for f in nc_files:
            # name of the activations file
            basename = os.path.basename(os.path.splitext(f)[0])
            act_file = "%s/%s.activations" % (out_dir, basename)
            # position in the list
            f_idx = nc_files.index(f)
            # save
            np.save(act_file, activations[f_idx])
        # return the output directory
        return out_dir

    def save_model(self, filename=None, comment=None, npz=True):
        """
        Save the model to a .h5 file which can be universally used and
        converted to .npz to create a madmom.ml.rnn.RNN instance.

        :param filename: save the model to this file
        :param comment:  optional comment for the model
        :param npz:      also convert to .npz format

        Note: If no filename is given, the filename of the .save file is used
              and the extension is set to .h5 or .npz respectively.

        """
        import h5py
        from .rnn import REVERSE
        # check if weights are present
        if not self.w:
            raise ValueError('please load a configuration file first')
        # set a default file name
        if filename is None:
            filename = "%s.h5" % os.path.splitext(self.filename)[0]
        # set the number of the output layer
        num_output = len(self.layer_sizes) - 1
        if num_output > 8:
            # FIXME: I know that works only with layer nums 0..9, have to come
            #        up with a proper solution.
            raise ValueError('too many layers, please fix me.')
        # save model
        with h5py.File(filename, 'w') as h5:
            # model attributes
            h5_m = h5.create_group('model')
            h5_m.attrs['type'] = 'RNN'
            if comment:
                h5_m.attrs['comment'] = comment
            # layers
            h5_l = h5.create_group('layer')
            # create a subgroup for each layer
            for layer in range(len(self.layer_sizes)):
                bidirectional = False
                # create group with layer number
                grp = h5_l.create_group(str(layer))
                # iterate over all weights
                for key in sorted(self.w.keys()):
                    # skip if it's not the right layer
                    if not key.startswith('layer_%s_' % layer):
                        continue
                    # get the weights
                    w = self.w[key]
                    name = None
                    if key.endswith('peephole_weights'):
                        name = 'peephole_weights'
                    elif key.endswith('recurrent_weights'):
                        name = 'recurrent_weights'
                    elif key.endswith('weights'):
                        name = 'weights'
                    elif key.endswith('bias'):
                        name = 'bias'
                    else:
                        ValueError('key %s not understood' % key)
                    # get the size of the layer to reshape it
                    layer_size = self.layer_sizes[layer]
                    # if we use LSTM units, align weights differently
                    if self.layer_types[layer] == 'lstm':
                        if 'peephole' in key:
                            # peephole connections
                            w = w.reshape(3 * layer_size, -1)
                        else:
                            # bias, weights and recurrent connections
                            w = w.reshape(4 * layer_size, -1)
                    # "normal" units
                    else:
                        w = w.reshape(layer_size, -1).T
                    # reverse
                    if key.startswith('layer_%s_0' % layer):
                        if re.sub('layer_%s_0' % layer, 'layer_%s_1' % layer,
                                  key) in self.w.keys():
                            name = '%s_%s' % (REVERSE, name)
                            bidirectional = True
                    # save the weights
                    grp.create_dataset(name, data=w.astype(np.float32))
                    # include the layer type as attribute
                    layer_type = self.layer_types[layer].capitalize()
                    if layer_type == 'Lstm':
                        layer_type = 'LSTM'
                    grp.attrs['type'] = str(layer_type)
                    # also for the reverse bidirectional layer if it exists
                    if bidirectional:
                        grp.attrs['%s_type' % REVERSE] = str(layer_type)
                # next layer
        # also convert to .npz
        if npz:
            from .io import convert_model
            convert_model(filename)


def test_save_files(nn_files, out_dir=None, file_set='test', threads=2,
                    verbose=False, fps=100):
    """
    Test the given set of files.

    :param nn_files: list with network files
    :param out_dir:  output directory for activations
    :param file_set: which set should be tested
                     file_set can be any of {train, val, test}
    :param threads:  number of working threads
    :param verbose:  be verbose
    :param fps:      frame rate of the Activations to be saved

    Note: If 'out_dir' is set and multiple network files contain the same
          files, the activations get averaged and saved to 'out_dir'.

          The activations are saved as Activations instances, i.e. .npz files
          which include a frame rate in fps (frames per second).

    """
    # FIXME: function only works if called in the directory of the NN file
    if out_dir is None:
        # test all NN files individually
        for nn_file in nn_files:
            nn = RnnlibConfigFile(nn_file)
            nn.test(file_set=file_set, threads=threads)
    else:
        # average all activations and output them in the given directory
        try:
            # create output directory
            os.mkdir(out_dir)
        except OSError:
            # directory exists already
            pass
        # get a list of all .nc files
        nc_files = []
        for nn_file in nn_files:
            nn = RnnlibConfigFile(nn_file)
            nc_files.extend(getattr(nn, "%s_files" % file_set))
        # remove duplicates
        nc_files = list(set(nc_files))
        # create a pool of workers
        work_queue, return_queue = create_pool(threads, verbose)
        # test each .nc files against the NN files if it is in the given set
        # Note: do not flip the order of the loops, otherwise files could be
        #       tested even if they were included in the train set!
        for nc_file in nc_files:
            # check in which NN files the .nc file is included
            nc_nn_files = []
            for nn_file in nn_files:
                nn = RnnlibConfigFile(nn_file)
                if nc_file in getattr(nn, "%s_files" % file_set):
                    nc_nn_files.append(nn_file)
            # test the .nc file against these networks
            activations = test_nc_files([nc_file], nc_nn_files, work_queue,
                                        return_queue)
            # name of the activations file
            basename = os.path.basename(os.path.splitext(nc_file)[0])
            act_file = "%s/%s" % (out_dir, basename)
            # cast the activations to an Activations instance (we only passed
            # one .nc file, so it's the first activation in the returned list)
            if verbose:
                print act_file
            Activations(activations[0], fps=fps).save(act_file)


def cross_validation(nc_files, filename, folds=8, randomize=True,
                     bidirectional=True, task='classification',
                     learn_rate=1e-4, layer_sizes=[25, 25, 25],
                     layer_type='lstm', momentum=0.9, optimizer='steepest',
                     splitting=None):
    """
    Creates RNNLIB config files for N-fold cross validation.

    :param nc_files:      use these .ns nc_files
    :param filename:      common base name for the config files
    :param folds:         number of folds
    :param randomize:     shuffle files before splitting
    :param bidirectional: use bidirectional neural networks
    :param task:          neural network task
    :param learn_rate:    learn rate to use
    :param layer_sizes:   sizes of the hidden layers
    :param layer_type:    hidden layer types
    :param momentum:      momentum for steepest descent
    :param optimizer:     which optimizer to use {'steepest, 'rprop'}
    :param splitting:     use pre-defined splittings

    Note: The 'splitting' can be either a dictionary with keys numerated from 0
          upwards or a list of files which contain one file per line.

    """
    # shuffle the files
    if randomize:
        import random
        random.shuffle(nc_files)
    # split into N parts
    splits = {}
    if isinstance(splitting, dict):
        # use the splitting as is
        splits = splitting
    if isinstance(splitting, list):
        from ..utils import match_file
        for fold, split_file in enumerate(splitting):
            with open(split_file, 'rb') as split:
                splits[fold] = []
                for line in split:
                    line = line.strip()
                    nc_file = match_file(line, nc_files, match_suffix='.nc')
                    splits[fold].append(nc_file[0])
    else:
        # use a standard splitting
        for fold in range(folds):
            splits[fold] = [f for i, f in enumerate(nc_files)
                            if i % folds == fold]
    # set the number of folds
    folds = len(splits)
    # create the config files
    assert folds >= 3, 'cannot create split with less than 3 folds.'
    for i in range(folds):
        config = RnnlibConfigFile()
        test_fold = np.nonzero(np.arange(i, i + folds) % folds == 0)[0]
        val_fold = np.nonzero(np.arange(i, i + folds) % folds == 1)[0]
        train_fold = np.nonzero(np.arange(i, i + folds) % folds >= 2)[0]
        # assign the sets
        config.test_files = splits[int(test_fold)]
        config.val_files = splits[int(val_fold)]
        config.train_files = []
        for j in train_fold.tolist():
            config.train_files.extend(splits[j])
        config.task = task
        config.bidirectional = bidirectional
        config.learn_rate = learn_rate
        config.layer_sizes = layer_sizes
        config.layer_types = [layer_type] * len(layer_sizes)
        config.momentum = momentum
        config.optimizer = optimizer
        # save the file
        config.save('%s_%s' % (filename, i))


def parser():
    """
    Create a parser and parse the arguments.

    :return: the parsed arguments

    """
    import argparse
    from ..utils import OverrideDefaultListAction
    # define parser
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description="""
    This module creates .nc files to be used by RNNLIB.
    Tests .save files produced by RNNLIB.

    """)
    # general options
    p.add_argument('files', nargs='+', help='files to be processed')
    p.add_argument('-v', dest='verbose', action='count',
                   help='increase verbosity level')
    p.add_argument('-o', dest='output', default=None,
                   help='output directory')
    p.add_argument('--threads', action='store', type=int,
                   default=multiprocessing.cpu_count(),
                   help='number of threads [default=%(default)i]')
    # .save file testing options
    g = p.add_argument_group('arguments for .save file testing')
    g.add_argument('--set', action='store', type=str, default='test',
                   help='use this set {train, val, test} [default='
                        '%(default)s]')
    # .nc file creation options
    g = p.add_argument_group('arguments for .nc file creation')
    g.add_argument('-a', dest='annotations', action=OverrideDefaultListAction,
                   default=['.onsets', '.beats', '.notes'],
                   help='annotations to use [default=%(default)s]')
    g.add_argument('--spec', dest='specs', default=[1024, 2048, 4096],
                   type=int, action=OverrideDefaultListAction,
                   help='spectrogram size(s) to use')
    g.add_argument('--split', default=None, type=float,
                   help='split files every N seconds')
    g.add_argument('--shift', default=None, type=float,
                   help='shift targets N seconds')
    # config file creation options
    g = p.add_argument_group('arguments for config file creation')
    g.add_argument('-c', dest='config', default=None,
                   help='config file base name')
    g.add_argument('--folds', default=8,
                   help='%(default)s-fold cross validation')
    g.add_argument('--splitting', action='append', default=None,
                   help='use this pre-defined splittings (argument needed '
                        'multiple times, one per splitting file)')
    g.add_argument('--random', action='store_true', default=False,
                   help='randomize splitting [default=%(default)s]')
    g.add_argument('--task', default='classification', type=str,
                   help='learning task [default=%(default)s]')
    g.add_argument('--bidirectional', action='store_true', default=False,
                   help='bidirectional network [default=%(default)s]')
    g.add_argument('--learn_rate', default=1e-4, type=float,
                   help='learn rate [default=%(default)s]')
    g.add_argument('--layer_sizes', default=[25, 25, 25], type=int,
                   help='layer sizes [default=%(default)s]')
    g.add_argument('--layer_type', default='tanh', type=str,
                   help='layer type [default=%(default)s]')
    g.add_argument('--momentum', default=0.9, type=float,
                   help='momentum for learning [default=%(default)s]')
    g.add_argument('--optimizer', default='steepest', type=str,
                   help='optimizer [default=%(default)s]')
    # add other options to the existing parser
    from ..audio.signal import Signal, FramedSignal
    from ..audio.filters import Filterbank
    from ..audio.spectrogram import Spectrogram
    Signal.add_arguments(p)
    FramedSignal.add_arguments(p, online=False)
    Filterbank.add_arguments(p)
    Spectrogram.add_arguments(p, log=True, mul=5, add=1)
    # parse arguments
    args = p.parse_args()
    # translate online/offline mode
    if args.online:
        args.origin = 'online'
    else:
        args.origin = 'offline'
    # print arguments
    if args.verbose >= 2:
        print args
    # return
    return args


def main():
    """
    Example script for testing RNNLIB .save files or creating .nc files
    understood by RNNLIB.

    """
    from ..audio.wav import Wav
    from ..audio.spectrogram import LogFiltSpec
    from ..utils import files, match_file, load_events, quantise_events

    # parse arguments
    args = parser()

    # create config file(s)
    if args.config:
        nc_files = files(args.files, '.nc')
        cross_validation(nc_files, args.config, folds=args.folds,
                         randomize=args.random, task=args.task,
                         bidirectional=args.bidirectional,
                         learn_rate=args.learn_rate,
                         layer_sizes=args.layer_sizes,
                         layer_type=args.layer_type, momentum=args.momentum,
                         optimizer=args.optimizer, splitting=args.splitting)

    # test all .save files
    save_files = files(args.files, '.save')
    test_save_files(save_files, out_dir=args.output, file_set=args.set,
                    threads=args.threads, verbose=args.verbose)

    # treat all files as annotation files
    ann_files = []
    for ext in args.annotations:
        ann_files.extend(files(args.files, ext))
    # create .nc files
    for f in ann_files:
        # split the extension of the input file
        annotation = os.path.splitext(f)[1]
        # get the matching wav file to the input file
        wav_files = match_file(f, files(args.files), annotation, '.wav')
        # no wav file found
        if len(wav_files) < 1:
            print "can't find audio file for %s" % f
            exit()
        # print file
        if args.verbose:
            print f
        # create a Wav object
        w = Wav(wav_files[0], mono=True, norm=args.norm)
        # spec
        nc_data = None
        for spec in args.specs:
            s = LogFiltSpec(w, frame_size=spec, fps=args.fps,
                            origin=args.origin, bands_per_octave=args.bands,
                            fmin=args.fmin, fmax=args.fmax,
                            mul=args.mul, add=args.add,
                            ratio=args.ratio, norm_filters=args.norm_filters)
            if nc_data is None:
                nc_data = np.hstack((s.spec, s.pos_diff))
            else:
                nc_data = np.hstack((nc_data, s.spec, s.pos_diff))
            # for creation of SuperFlux .nc files:
            # from ..features.onsets import SpectralOnsetDetection as sodf
            # nc_data = sodf.from_data(s, fps=args.fps).superflux()
            # nc_data = np.atleast_2d(nc_data).T

        # targets
        if f.endswith('.notes'):
            # load notes
            from ..features.notes import load_notes
            notes = load_notes(f)
            # shift the notes if needed
            if args.shift:
                notes[:, 0] += args.shift
            # convert to frame numbers
            notes[:, 0] *= float(args.fps)
            # set the range of MIDI notes to 0..88
            notes[:, 2] -= 21
            # set the targets
            targets = np.zeros((s.num_frames, 88))
            for note in notes:
                try:
                    targets[int(note[0]), int(note[2])] = 1
                except IndexError:
                    pass
        else:
            # load events (onset/beat)
            targets = load_events(f)
            targets = quantise_events(targets, args.fps, length=s.num_frames,
                                      shift=args.shift)
        # tags
        tags = ("file=%s | fps=%s | specs=%s | bands=%s | fmin=%s | fmax=%s | "
                "norm_filter=%s | log=%s | mul=%s | add=%s | ratio=%s | "
                "shift=%s" %
                (f, args.fps, args.specs, args.bands, args.fmin, args.fmax,
                 args.norm_filters, args.log, args.mul, args.add, args.ratio,
                 args.shift))
        # .nc file name
        if args.output:
            nc_file = "%s/%s" % (args.output, os.path.basename(f))
        else:
            nc_file = "%s" % os.path.abspath(f)
        # split files
        if args.split is None:
            # create a .nc file
            create_nc_file(nc_file + '.nc', nc_data, targets, tags)
        else:
            # length of one part
            length = int(args.split * args.fps)
            # number of parts
            parts = int(np.ceil(s.num_frames / float(length)))
            digits = int(np.ceil(np.log10(parts + 1)))
            if digits > 4:
                raise ValueError('please chose longer splits')
            for i in range(parts):
                nc_part_file = "%s.part%04d.nc" % (nc_file, i)
                start = i * length
                stop = start + length
                if stop > s.num_frames:
                    stop = s.num_frames
                part_tags = "%s | part=%s | start=%s | stop=%s" %\
                            (tags, i, start, stop - 1)
                create_nc_file(nc_part_file, nc_data[start:stop],
                               targets[start:stop], part_tags)


if __name__ == '__main__':
    main()
