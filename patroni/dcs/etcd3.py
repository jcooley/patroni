from __future__ import absolute_import
import base64
import etcd
import json
import logging
import os
import six
import time

from patroni.dcs import ClusterConfig, Cluster, Failover, Leader, Member, SyncState, TimelineHistory
from patroni.dcs.etcd import AbstractEtcdClientWithFailover, AbstractEtcd, catch_etcd_errors
from patroni.exceptions import DCSError
from patroni.utils import Retry, RetryFailedError

logger = logging.getLogger(__name__)


# Old grpc-gateway sometimes sends double 'transfer-encoding: chunked' headers, it breaks the HTTPConnection
def dedup_addheader(self, key, value):
    prev = self.dict.get(key)
    if prev is None:
        self.dict[key] = value
    elif key != 'transfer-encoding' or prev != value:
        combined = ", ".join((prev, value))
        self.dict[key] = combined


six.moves.http_client.HTTPMessage.addheader = dedup_addheader


class EtcdError(DCSError):
    pass


# google.golang.org/grpc/codes
GRPCCode = type('Enum', (), {'OK': 0, 'Canceled': 1, 'Unknown': 2, 'InvalidArgument': 3, 'DeadlineExceeded': 4,
                             'NotFound': 5, 'AlreadyExists': 6, 'PermissionDenied': 7, 'ResourceExhausted': 8,
                             'FailedPrecondition': 9, 'Aborted': 10, 'OutOfRange': 11, 'Unimplemented': 12,
                             'Internal': 13, 'Unavailable': 14, 'DataLoss': 15, 'Unauthenticated': 16})
GRPCcodeToText = {v: k for k, v in GRPCCode.__dict__.items() if not k.startswith('__') and isinstance(v, int)}


class Etcd3Exception(etcd.EtcdException):
    pass


class Etcd3ClientError(Etcd3Exception):

    def __init__(self, code=None, error=None, status=None):
        if not hasattr(self, 'error'):
            self.error = error.strip()
        self.codeText = GRPCcodeToText.get(code)
        self.status = status

    def __repr__(self):
        return "<{0} error: '{1}', code: {2}>".format(self.__class__.__name__, self.error, self.code)

    __str__ = __repr__

    def as_dict(self):
        return {'error': self.error, 'code': self.code, 'codeText': self.codeText, 'status': self.status}

    @classmethod
    def get_subclasses(cls):
        for subclass in cls.__subclasses__():
            for subsubclass in subclass.get_subclasses():
                yield subsubclass
            yield subclass


class Unknown(Etcd3ClientError):
    code = GRPCCode.Unknown


class InvalidArgument(Etcd3ClientError):
    code = GRPCCode.InvalidArgument


class DeadlineExceeded(Etcd3ClientError):
    code = GRPCCode.DeadlineExceeded
    error = "context deadline exceeded"


class NotFound(Etcd3ClientError):
    code = GRPCCode.NotFound


class FailedPrecondition(Etcd3ClientError):
    code = GRPCCode.FailedPrecondition


class OutOfRange(Etcd3ClientError):
    code = GRPCCode.OutOfRange


class Unavailable(Etcd3ClientError):
    code = GRPCCode.Unavailable


# https://github.com/etcd-io/etcd/blob/master/etcdserver/api/v3rpc/rpctypes/error.go
class EmptyKey(InvalidArgument):
    error = "etcdserver: key is not provided"


class KeyNotFound(InvalidArgument):
    error = "etcdserver: key not found"


class ValueProvided(InvalidArgument):
    error = "etcdserver: value is provided"


class LeaseProvided(InvalidArgument):
    error = "etcdserver: lease is provided"


class TooManyOps(InvalidArgument):
    error = "etcdserver: too many operations in txn request"


class DuplicateKey(InvalidArgument):
    error = "etcdserver: duplicate key given in txn request"


class Compacted(OutOfRange):
    error = "etcdserver: mvcc: required revision has been compacted"


class FutureRev(OutOfRange):
    error = "etcdserver: mvcc: required revision is a future revision"


class NoSpace(Etcd3ClientError):
    code = GRPCCode.ResourceExhausted
    error = "etcdserver: mvcc: database space exceeded"


class LeaseNotFound(NotFound):
    error = "etcdserver: requested lease not found"


class LeaseExist(FailedPrecondition):
    error = "etcdserver: lease already exists"


class LeaseTTLTooLarge(OutOfRange):
    error = "etcdserver: too large lease TTL"


class MemberExist(FailedPrecondition):
    error = "etcdserver: member ID already exist"


class PeerURLExist(FailedPrecondition):
    error = "etcdserver: Peer URLs already exists"


class MemberNotEnoughStarted(FailedPrecondition):
    error = "etcdserver: re-configuration failed due to not enough started members"


class MemberBadURLs(InvalidArgument):
    error = "etcdserver: given member URLs are invalid"


class MemberNotFound(NotFound):
    error = "etcdserver: member not found"


class MemberNotLearner(FailedPrecondition):
    error = "etcdserver: can only promote a learner member"


class MemberLearnerNotReady(FailedPrecondition):
    error = "etcdserver: can only promote a learner member which is in sync with leader"


class TooManyLearners(FailedPrecondition):
    error = "etcdserver: too many learner members in cluster"


class RequestTooLarge(InvalidArgument):
    error = "etcdserver: request is too large"


class TooManyRequests(Etcd3ClientError):
    code = GRPCCode.ResourceExhausted
    error = "etcdserver: too many requests"


class RootUserNotExist(FailedPrecondition):
    error = "etcdserver: root user does not exist"


class RootRoleNotExist(FailedPrecondition):
    error = "etcdserver: root user does not have root role"


class UserAlreadyExist(FailedPrecondition):
    error = "etcdserver: user name already exists"


class UserEmpty(InvalidArgument):
    error = "etcdserver: user name is empty"


class UserNotFound(FailedPrecondition):
    error = "etcdserver: user name not found"


class RoleAlreadyExist(FailedPrecondition):
    error = "etcdserver: role name already exists"


class RoleNotFound(FailedPrecondition):
    error = "etcdserver: role name not found"


class RoleEmpty(InvalidArgument):
    error = "etcdserver: role name is empty"


class AuthFailed(InvalidArgument):
    error = "etcdserver: authentication failed, invalid user ID or password"


class PermissionDenied(Etcd3ClientError):
    code = GRPCCode.PermissionDenied
    error = "etcdserver: permission denied"


class RoleNotGranted(FailedPrecondition):
    error = "etcdserver: role is not granted to the user"


class PermissionNotGranted(FailedPrecondition):
    error = "etcdserver: permission is not granted to the role"


class AuthNotEnabled(FailedPrecondition):
    error = "etcdserver: authentication is not enabled"


class InvalidAuthToken(Etcd3ClientError):
    code = GRPCCode.Unauthenticated
    error = "etcdserver: invalid auth token"


class InvalidAuthMgmt(InvalidArgument):
    error = "etcdserver: invalid auth management"


class NoLeader(Unavailable):
    error = "etcdserver: no leader"


class NotLeader(FailedPrecondition):
    error = "etcdserver: not leader"


class LeaderChanged(Unavailable):
    error = "etcdserver: leader changed"


class NotCapable(Unavailable):
    error = "etcdserver: not capable"


class Stopped(Unavailable):
    error = "etcdserver: server stopped"


class Timeout(Unavailable):
    error = "etcdserver: request timed out"


class TimeoutDueToLeaderFail(Unavailable):
    error = "etcdserver: request timed out, possibly due to previous leader failure"


class TimeoutDueToConnectionLost(Unavailable):
    error = "etcdserver: request timed out, possibly due to connection lost"


class Unhealthy(Unavailable):
    error = "etcdserver: unhealthy cluster"


class Corrupt(Etcd3ClientError):
    code = GRPCCode.DataLoss
    error = "etcdserver: corrupt cluster"


class BadLeaderTransferee(FailedPrecondition):
    error = "etcdserver: bad leader transferee"


errStringToClientError = {s.error: s for s in Etcd3ClientError.get_subclasses() if hasattr(s, 'error')}
errCodeToClientError = {s.code: s for s in Etcd3ClientError.get_subclasses() if not hasattr(s, 'error')}


def _raise_for_status(response):
    if response.status < 400:
        return
    data = response.data.decode('utf-8')
    try:
        data = json.loads(data)
        error = data.get('error')
        if isinstance(error, dict):  # streaming response
            code = error['grpc_code']
            error = error['message']
        else:
            code = data.get('code')
    except Exception:
        error = data
        code = GRPCCode.Unknown
    err = errStringToClientError.get(error) or errCodeToClientError.get(code) or Unknown
    raise err(error, code, response.status)


class Etcd3Client(AbstractEtcdClientWithFailover):

    def _prepare_request(self, params=None):
        kwargs = self._build_request_parameters()
        kwargs['preload_content'] = False
        if params is None:
            kwargs['body'] = ''
        else:
            kwargs['body'] = json.dumps(params)
            kwargs['headers']['Content-Type'] = 'application/json'
        return self.http.urlopen, kwargs

    @staticmethod
    def _handle_server_response(response):
        _raise_for_status(response)
        try:
            return json.loads(response.data.decode('utf-8'))
        except (TypeError, ValueError, UnicodeError) as e:
            raise etcd.EtcdException('Server response was not valid JSON: %r' % e)

    def _ensure_version_prefix(self):
        if self.version_prefix != '/v3':
            request_executor, kwargs = self._prepare_request()
            response = request_executor(self._MGET, self._base_uri + '/version', **kwargs)
            cluster_version = self._handle_server_response(response)['etcdcluster']
            cluster_version = tuple(int(x) for x in cluster_version.split('.'))
            if cluster_version < (3, 3):
                self.version_prefix = '/v3alpha'
            elif cluster_version < (3, 4):
                self.version_prefix = '/v3beta'
            else:
                self.version_prefix = '/v3'

    def _refresh_machines_cache(self):
        self._ensure_version_prefix()
        super(Etcd3Client, self)._refresh_machines_cache()

    def _get_members(self):
        request_executor, kwargs = self._prepare_request({})
        resp = request_executor(self._MPOST, self._base_uri + self.version_prefix + '/cluster/member/list', **kwargs)
        members = self._handle_server_response(resp)['members']
        return set(url for member in members for url in member['clientURLs'])

    def call_rpc(self, method, fields=None):
        return self.api_execute(self.version_prefix + method, self._MPOST, fields)

    @staticmethod
    def to_bytes(v):
        return v if isinstance(v, bytes) else v.encode('utf-8')

    @staticmethod
    def base64_encode(v):
        return base64.b64encode(Etcd3Client.to_bytes(v)).decode('utf-8')

    def build_range_request(self, key, range_end=None):
        fields = {'key': self.base64_encode(key)}
        if range_end:
            fields['range_end'] = self.base64_encode(range_end)
        return fields

    def range(self, key, range_end=None):
        return self.call_rpc('/kv/range', self.build_range_request(key, range_end))

    def increment_last_byte(self, v):
        v = bytearray(self.to_bytes(v))
        v[-1] += 1
        return bytes(v)

    def prefix(self, key):
        return self.range(key, self.increment_last_byte(key))

    def lease_grant(self, ttl):
        return self.call_rpc('/lease/grant', {'TTL': ttl})['ID']

    def lease_keepalive(self, ID):
        return self.call_rpc('/lease/keepalive', {'ID': ID}).get('result', {}).get('TTL')

    def put(self, key, value, lease=None, create_revision=None, mod_revision=None):
        fields = {'key': self.base64_encode(key), 'value': self.base64_encode(value)}
        if lease:
            fields['lease'] = lease
        if create_revision is not None:
            compare = {'target': 'CREATE', 'create_revision': create_revision}
        elif mod_revision is not None:
            compare = {'target': 'MOD', 'mod_revision': mod_revision}
        else:
            return self.call_rpc('/kv/put', fields)
        compare['key'] = fields['key']
        return self.call_rpc('/kv/txn', {'compare': [compare], 'success': [{'request_put': fields}]}).get('succeeded')

    def deleterange(self, key, range_end=None, mod_revision=None):
        fields = self.build_range_request(key, range_end)
        if mod_revision is None:
            return self.call_rpc('/kv/deleterange', fields)
        compare = {'target': 'MOD', 'mod_revision': mod_revision, 'key': fields['key']}
        ret = self.call_rpc('/kv/txn', {'compare': [compare], 'success': [{'request_delete_range': fields}]})
        return ret.get('succeeded')

    def deleteprefix(self, key):
        return self.deleterange(key, self.increment_last_byte(key))


class Etcd3(AbstractEtcd):

    def __init__(self, config):
        super(Etcd3, self).__init__(config, Etcd3Client, Etcd3ClientError)
        self._retry = Retry(deadline=config['retry_timeout'], max_delay=1, max_tries=-1,
                            retry_exceptions=(DeadlineExceeded, Unavailable))
        self.__do_not_watch = False
        self._lease = None
        self._last_lease_refresh = 0
        if not self._ctl:
            self.create_lease()

    def set_ttl(self, ttl):
        self.__do_not_watch = super(Etcd3, self).set_ttl(ttl)
        if self.__do_not_watch:
            self._lease = None

    def _do_refresh_lease(self):
        if self._lease and self._last_lease_refresh + self._loop_wait > time.time():
            return False

        if self._lease and not self._client.lease_keepalive(self._lease):
            self._lease = None

        ret = not self._lease
        if ret:
            self._lease = self._client.lease_grant(self._ttl)

        self._last_lease_refresh = time.time()
        return ret

    def refresh_lease(self):
        try:
            return self.retry(self._do_refresh_lease)
        except (Etcd3ClientError, RetryFailedError):
            logger.exception('refresh_lease')
        raise EtcdError('Failed ro keepalive/grant lease')

    def create_lease(self):
        while not self._lease:
            try:
                self.refresh_lease()
            except EtcdError:
                logger.info('waiting on etcd')
                time.sleep(5)

    @staticmethod
    def member(node):
        return Member.from_node(node['mod_revision'], os.path.basename(node['key']), node['lease'], node['value'])

    def _load_cluster(self):
        def base64_decode(v):
            return base64.b64decode(v).decode('utf-8')

        cluster = None
        try:
            path = self.client_path('')
            result = self.retry(self._client.prefix, path)
            nodes = {}
            for node in result.get('kvs', []):
                node['key'] = base64_decode(node['key'])
                node['value'] = base64_decode(node.get('value', ''))
                node['lease'] = node.get('lease')
                nodes[node['key'][len(path):].lstrip('/')] = node

            # get initialize flag
            initialize = nodes.get(self._INITIALIZE)
            initialize = initialize and initialize['value']

            # get global dynamic configuration
            config = nodes.get(self._CONFIG)
            config = config and ClusterConfig.from_node(config['mod_revision'], config['value'])

            # get timeline history
            history = nodes.get(self._HISTORY)
            history = history and TimelineHistory.from_node(history['mod_revision'], history['value'])

            # get last leader operation
            last_leader_operation = nodes.get(self._LEADER_OPTIME)
            last_leader_operation = 0 if last_leader_operation is None else int(last_leader_operation['value'])

            # get list of members
            members = [self.member(n) for k, n in nodes.items() if k.startswith(self._MEMBERS) and k.count('/') == 1]

            # get leader
            leader = nodes.get(self._LEADER)
            if leader:
                member = Member(-1, leader['value'], None, {})
                member = ([m for m in members if m.name == leader['value']] or [member])[0]
                leader = Leader(leader['mod_revision'], leader['lease'], member)

            # failover key
            failover = nodes.get(self._FAILOVER)
            if failover:
                failover = Failover.from_node(failover['mod_revision'], failover['value'])

            # get synchronization state
            sync = nodes.get(self._SYNC)
            sync = SyncState.from_node(sync and sync['mod_revision'], sync and sync['value'])

            cluster = Cluster(initialize, config, leader, last_leader_operation, members, failover, sync, history)
        except Exception as e:
            self._handle_exception(e, 'get_cluster', raise_ex=EtcdError('Etcd is not responding properly'))
        self._has_failed = False
        return cluster

    @catch_etcd_errors
    def touch_member(self, data, permanent=False):
        if not permanent:
            self.refresh_lease()
        # TODO: add caching
        data = json.dumps(data, separators=(',', ':'))
        try:
            return self._client.put(self.member_path, data, None if permanent else self._lease)
        except LeaseNotFound:
            self._lease = None
            logger.error('Our lease disappeared from Etcd, can not "touch_member"')

    @catch_etcd_errors
    def take_leader(self):
        return self.retry(self._client.put, self.leader_path, self._name, self._lease)

    def _do_attempt_to_acquire_leader(self, permanent):
        try:
            return self.retry(self._client.put, self.leader_path, self._name, None if permanent else self._lease, 0)
        except LeaseNotFound:
            self._lease = None
            logger.error('Our lease disappeared from Etcd. Will try to get a new one and retry attempt')
            self.refresh_lease()
            return self.retry(self._client.put, self.leader_path, self._name, None if permanent else self._lease, 0)

    def attempt_to_acquire_leader(self, permanent=False):
        if not self._lease and not permanent:
            self.refresh_lease()

        ret = self._do_attempt_to_acquire_leader(permanent)
        if not ret:
            logger.info('Could not take out TTL lock')
        return ret

    @catch_etcd_errors
    def set_failover_value(self, value, index=None):
        return self._client.put(self.failover_path, value, mod_revision=index)

    @catch_etcd_errors
    def set_config_value(self, value, index=None):
        return self._client.put(self.config_path, value, mod_revision=index)

    @catch_etcd_errors
    def _write_leader_optime(self, last_operation):
        return self._client.put(self.leader_optime_path, last_operation)

    @catch_etcd_errors
    def _update_leader(self):
        # XXX: what if leader lease doesn't match?
        if not self._lease:
            self.refresh_lease()
            self.take_leader()
        elif self.retry(self._client.lease_keepalive, self._lease):
            self._last_lease_refresh = time.time()
        return bool(self._lease)

    @catch_etcd_errors
    def initialize(self, create_new=True, sysid=""):
        return self.retry(self._client.put, self.initialize_path, sysid, None, 0 if create_new else None)

    @catch_etcd_errors
    def delete_leader(self):
        cluster = self.cluster
        if cluster and isinstance(cluster.leader, Leader) and cluster.leader.name == self._name:
            return self._client.deleterange(self.leader_path, mod_revision=cluster.leader.index)

    @catch_etcd_errors
    def cancel_initialization(self):
        return self.retry(self._client.deleterange, self.initialize_path)

    @catch_etcd_errors
    def delete_cluster(self):
        return self.retry(self._client.deleteprefix, self.client_path(''))

    @catch_etcd_errors
    def set_history_value(self, value):
        return self._client.put(self.history_path, value)

    @catch_etcd_errors
    def set_sync_state_value(self, value, index=None):
        return self.retry(self._client.put, self.sync_path, value, mod_revision=index)

    @catch_etcd_errors
    def delete_sync_state(self, index=None):
        return self.retry(self._client.deleterange, self.sync_path, mod_revision=index)

    def watch(self, leader_index, timeout):
        if self.__do_not_watch:
            self.__do_not_watch = False
            return True

        try:
            return super(Etcd3, self).watch(None, timeout)
        finally:
            self.event.clear()
