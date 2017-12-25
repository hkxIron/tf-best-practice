import json

import dask.bag as db
import tensorflow as tf
from tensorflow.contrib.learn import ModeKeys
from tensorflow.python.data import Dataset

from config import APP_CONFIG, LOGGER, MODEL_PARAM
from .logger import JobContext


class InputData:
    def __init__(self):
        LOGGER.info('maximum length of training sent: %d' % MODEL_PARAM.len_threshold)

        with JobContext('indexing all chars/chatrooms/users...', LOGGER):
            b = db.read_text(APP_CONFIG.data_file).map(json.loads)
            msg_stream = b.filter(lambda x: x['msgType'] == 'Text').filter(
                lambda x: len(x['text']) <= MODEL_PARAM.len_threshold)
            text_stream = msg_stream.pluck('text').distinct()
            chatroom_stream = msg_stream.pluck('chatroomName').distinct()
            user_stream = msg_stream.pluck('fromUser').distinct()

            all_chars = sorted(text_stream.flatten().filter(lambda x: x).distinct())
            all_rooms = sorted(chatroom_stream.compute())
            all_users = sorted(user_stream.compute())

            unknown_char_idx = 0
            reserved_chars = 1
            char2int_map = {c: idx + reserved_chars for idx, c in enumerate(all_chars)}

            unknown_room_idx = len(char2int_map)
            reserved_chars += len(char2int_map) + 1
            room2int_map = {c: idx + reserved_chars for idx, c in enumerate(all_rooms)}

            unknown_user_idx = len(room2int_map)
            reserved_chars += len(room2int_map) + 1
            user2int_map = {c: idx + reserved_chars for idx, c in enumerate(all_users)}

            reserved_chars += len(user2int_map)

        with JobContext('computing some statistics...', LOGGER):
            num_sent = text_stream.count().compute()
            num_room = chatroom_stream.count().compute()
            num_user = user_stream.count().compute()
            num_char = len(char2int_map) + 1
            LOGGER.info('# sentences: %d' % num_sent)
            LOGGER.info('# chars: %d' % num_char)
            LOGGER.info('# rooms: %d' % num_room)
            LOGGER.info('# users: %d' % num_user)
            LOGGER.info('# symbols: %d' % reserved_chars)

        with JobContext('building dataset...', LOGGER):
            d = (msg_stream.map(lambda x: (
                [char2int_map.get(c, unknown_char_idx) for c in x['text']],
                room2int_map.get(x['chatroomName'], unknown_room_idx),
                user2int_map.get(x['fromUser'], unknown_user_idx))))

            X = d.pluck(0).compute(), d.pluck(1).compute(), d.pluck(2).compute()

            def gen():
                for i in range(len(X[0])):
                    yield X[0][i], X[1][i], X[2][i]

            ds = Dataset.from_generator(generator=gen, output_types=(tf.int32, tf.int32, tf.int32),
                                        output_shapes=([None], [], [])).shuffle(buffer_size=10000)  # type: Dataset
            self.eval_ds = ds.take(MODEL_PARAM.num_eval)
            self.train_ds = ds.skip(MODEL_PARAM.num_eval)

        self.num_char = num_char
        self.num_reserved_char = reserved_chars
        self.num_sent = num_sent
        self.num_room = num_room
        self.num_user = num_user
        self.char2int = char2int_map
        self.user2int = user2int_map
        self.room2int = room2int_map
        self.int2char = {i: c for c, i in char2int_map.items()}
        self.int2user = {i: c for c, i in user2int_map.items()}
        self.int2room = {i: c for c, i in room2int_map.items()}
        self.unknown_char_idx = unknown_char_idx
        self.unknown_user_idx = unknown_user_idx
        self.unknown_room_idx = unknown_room_idx

        MODEL_PARAM.add_hparam('num_char', num_char)

        LOGGER.info('data loading finished!')

    def input_fn(self, mode: ModeKeys):
        return {
                   ModeKeys.TRAIN:
                       lambda: self.train_ds.repeat(MODEL_PARAM.num_epoch).padded_batch(MODEL_PARAM.batch_size,
                                                                                        padded_shapes=([None], [], [])),
                   ModeKeys.EVAL:
                       lambda: self.eval_ds.padded_batch(MODEL_PARAM.batch_size, padded_shapes=([None], [], [])),
                   ModeKeys.INFER: lambda: Dataset.range(1)
               }[mode]().make_one_shot_iterator().get_next(), None

    def decode(self, predictions):
        return [''.join([self.int2char[j] for j in x]) for x in predictions]
