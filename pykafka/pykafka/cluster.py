import logging
import time
import random

from .broker import Broker
from .topic import Topic
from .protocol import ConsumerMetadataRequest, ConsumerMetadataResponse


logger = logging.getLogger(__name__)


class Cluster(object):
    """Cluster implementation used to populate the KafkaClient."""

    def __init__(self,
                 hosts,
                 handler,
                 socket_timeout_ms=30 * 1000,
                 offsets_channel_socket_timeout_ms=10 * 1000,
                 socket_receive_buffer_bytes=64 * 1024,
                 exclude_internal_topics=True):
        self._seed_hosts = hosts
        self._socket_timeout_ms = socket_timeout_ms
        self._offsets_channel_socket_timeout_ms = offsets_channel_socket_timeout_ms
        self._handler = handler
        self._brokers = {}
        self._topics = {}
        self._socket_receive_buffer_bytes = socket_receive_buffer_bytes
        self._exclude_internal_topics = exclude_internal_topics
        self.update()

    @property
    def brokers(self):
        return self._brokers

    @property
    def topics(self):
        return self._topics

    @property
    def handler(self):
        return self._handler

    def _get_metadata(self):
        """Get fresh cluster metadata from a broker"""
        # Works either on existing brokers or seed_hosts list
        if self.brokers:
            brokers = self.brokers.values()
        else:
            brokers = self._seed_hosts.split(',')

        for broker in brokers:
            try:
                if isinstance(broker, basestring):
                    h, p = broker.split(':')
                    broker = Broker(-1, h, p, self._handler, self._socket_timeout_ms,
                                    self._offsets_channel_socket_timeout_ms,
                                    buffer_size=self._socket_receive_buffer_bytes)
                return broker.request_metadata()
            # TODO: Change to typed exception
            except Exception:
                logger.exception('Unable to connect to broker %s', broker)
                raise
        raise Exception('Unable to connect to a broker to fetch metadata.')

    def _update_brokers(self, broker_metadata):
        """Update brokers with fresh metadata.

        :param broker_metadata: Metadata for all brokers
        :type broker_metadata: Dict of `{name: metadata}` where `metadata is
            :class:`kafka.pykafka.protocol.BrokerMetadata`
        """
        # FIXME: A cluster with no topics returns no brokers in metadata
        # Remove old brokers
        removed = set(self._brokers.keys()) - set(broker_metadata.keys())
        for id_ in removed:
            logger.info('Removing broker %s', self._brokers[id_])
            self._brokers.pop(id_)
        # Add/update current brokers
        for id_, meta in broker_metadata.iteritems():
            if id_ not in self._brokers:
                logger.info('Discovered broker %s:%s', meta.host, meta.port)
                self._brokers[id_] = Broker.from_metadata(
                    meta, self._handler, self._socket_timeout_ms,
                    self._offsets_channel_socket_timeout_ms,
                    buffer_size=self._socket_receive_buffer_bytes
                )
            else:
                broker = self._brokers[id_]
                if meta.host == broker.host and meta.port == broker.port:
                    continue  # no changes
                # TODO: Can brokers update? Seems like a problem if so.
                #       Figure out and implement update/disconnect/reconnect if
                #       needed.
                raise Exception('Broker host/port change detected! %s', broker)

    def _update_topics(self, metadata):
        """Update topics with fresh metadata.

        :param metadata: Metadata for all topics
        :type metadata: Dict of `{name, metadata}` where `metadata` is
            :class:`kafka.pykafka.protocol.TopicMetadata`
        """
        # Remove old topics
        removed = set(self._topics.keys()) - set(metadata.keys())
        for name in removed:
            logger.info('Removing topic %s', self._topics[name])
            self._topics.pop(name)
        # Add/update partition information
        for name, meta in metadata.iteritems():
            if not self._should_exclude_topic(name):
                if name not in self._topics:
                    self._topics[name] = Topic(self, meta)
                    logger.info('Discovered topic %s', self._topics[name])
                else:
                    self._topics[name].update(meta)

    def _should_exclude_topic(self, topic_name):
        """Return a boolean indicating whether this topic should be exluded
        """
        if not self._exclude_internal_topics:
            return False
        return topic_name.startswith("__")

    def get_offset_manager(self, consumer_group):
        """Get the broker designated as the offset manager for this consumer
            group

        Based on Step 1 at https://cwiki.apache.org/confluence/display/KAFKA/Committing+and+fetching+consumer+offsets+in+Kafka

        :param consumer_group: the name of the consumer group
        :type consumer_group: str
        """
        # arbitrarily choose a broker, since this request can go to any
        broker = self.brokers[random.choice(self.brokers.keys())]
        backoff, retries = 2, 0
        MAX_RETRIES = 3
        while True:
            try:
                retries += 1
                req = ConsumerMetadataRequest(consumer_group)
                future = broker.handler.request(req)
                res = future.get(ConsumerMetadataResponse)
            except Exception:
                logger.debug('Error discovering offset manager. Sleeping for {}s'.format(backoff))
                if retries < MAX_RETRIES:
                    time.sleep(backoff)
                    backoff = backoff ** 2
                else:
                    raise
            else:
                coordinator = self.brokers.get(res.coordinator_id, None)
                if coordinator is None:
                    raise Exception('Coordinator broker with id {} not found'.format(res.coordinator_id))
                return coordinator

    def update(self):
        """Update known brokers and topics."""
        metadata = self._get_metadata()
        self._update_brokers(metadata.brokers)
        self._update_topics(metadata.topics)
        # N.B.: Partitions are updated as part of Topic updates.
