"""Offline behavioral regression tests; never connect to a real cluster."""
import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from cluster import Cluster, prepare_spec, RemoteExecutionTimeout
from registry import Registry, profile_identity


def profile():
    return {'portal':'https://cluster.example.invalid:443','account':'example-user',
            'image':'registry.example.invalid/pytorch:stable','persistent_root':'/data/runs',
            'development_group_id':'example-group','card_type':'Example GPU','framework':'pytorch',
            'ssh_host_fingerprint':'SHA256:example','_path':'/example/profile.json'}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.cluster=Cluster(self.root/'state',profile())
        self.spec=prepare_spec({'run_id':'example','argv':['/bin/true'],'gpus':0,'cpus':1},profile())
        self.cluster.registry.reserve('example',self.spec,'run')

    def fake_client(self):
        self.cluster._client=Mock()
        self.cluster._user={'userId':'example-owner'}
        self.cluster.resources=Mock(return_value=[{'node':'example-node','available_cpus':500,
                                                   'available_gpus':25,'card_type':'Example GPU'}])
        return self.cluster._client

    def allocated(self,**values):
        self.cluster.registry.update('example',resource_id='example-env',state='RUNNING',**values)

    def test_path_traversal_and_changed_source_rejected(self):
        source=self.root/'main.py'; source.write_text('print(1)\n')
        request={'run_id':'paths','argv':['/bin/true'],'uploads':[{'local':str(source),'remote':'{run_dir}/../escape.py'}]}
        with self.assertRaises(ValueError):prepare_spec(request,profile())
        request['uploads'][0]['remote']='{run_dir}/main.py'
        prepared=prepare_spec(request,profile())
        source.write_text('print(2)\n')
        with self.assertRaises(ValueError):prepare_spec(prepared,profile())
        with patch.object(self.cluster,'ssh',side_effect=AssertionError('connected before hash validation')):
            self.cluster.registry.reserve('paths',prepared,'run')
            self.cluster.registry.update('paths',resource_id='fake')
            with self.assertRaises(ValueError):self.cluster.launch('paths')

    def test_state_directory_cannot_cross_accounts(self):
        root=self.root/'scoped'
        Registry(root,profile_identity(profile()))
        changed=dict(profile(),account='another-user')
        with self.assertRaises(ValueError):Registry(root,profile_identity(changed))

    def test_per_run_locks_do_not_block_unrelated_runs(self):
        registry=self.cluster.registry
        registry.reserve('other',dict(self.spec,run_id='other'),'run')
        completed=threading.Event()
        with registry.operation('example','allocation'):
            thread=threading.Thread(target=lambda:(registry.update('other',state='READY'),completed.set()))
            thread.start()
            self.assertTrue(completed.wait(1),'Unrelated run blocked behind allocation')
            thread.join()
        with registry.controller('example') as owner:
            self.assertTrue(owner)
            self.assertTrue(registry.controller_active('example'))
        self.assertFalse(registry.controller_active('example'))

    def test_offline_logs_and_terminal_run_do_not_authenticate(self):
        local=self.cluster.registry.root/'example_files';local.mkdir()
        (local/'stdout.log').write_text('one\ntwo\nthree\n')
        self.cluster.registry.update('example',state='SUCCEEDED',resource_released=True)
        with patch('cluster.get_password',side_effect=AssertionError('unexpected authentication')):
            self.assertEqual(self.cluster.logs('example',2)['stdout.log'],'two\nthree')
            self.assertEqual(self.cluster.run(self.spec)['state'],'SUCCEEDED')

    def test_ambiguous_allocation_is_not_repeated(self):
        client=self.fake_client();client.call.side_effect=TimeoutError('simulated')
        with self.assertRaises(TimeoutError):self.cluster.create_environment('example')
        self.assertEqual(self.cluster.registry.get('example')['state'],'UNKNOWN')
        with self.assertRaises(RuntimeError):self.cluster.create_environment('example')
        self.assertEqual(client.call.call_count,1)

    def test_cancel_before_allocation_prevents_post(self):
        client=self.fake_client()
        self.cluster.cancel('example')
        with self.assertRaises(InterruptedError):self.cluster.create_environment('example')
        client.call.assert_not_called()

    def test_pending_empty_detail_waits_for_verified_running_allocation(self):
        self.allocated();self.fake_client()
        expected=dict(self.spec,gpus=1,card_type='Example GPU')
        self.cluster.registry.update('example',spec=expected)
        self.cluster.environment=Mock(side_effect=[
            {'userId':'example-owner','wpStatus':'Pending'},
            {'userId':'example-owner','wpStatus':'ImagePulling','cpu':None,'image':None,'acceleratorCard':0,'acceleratorCardType':''},
            {'userId':'example-owner','wpStatus':'Running','cpu':1,'image':expected['image'],'acceleratorCard':1,'acceleratorCardType':'Example GPU','acceleratorCardKind':'GPU'}])
        with patch('cluster.time.sleep',return_value=None):
            record=self.cluster.wait_environment('example')
        self.assertEqual(record['state'],'READY');self.assertEqual(self.cluster.environment.call_count,3)
        self.cluster.environment=Mock(return_value={'userId':'example-owner','wpStatus':'Running','cpu':1,'image':expected['image'],'acceleratorCard':0,'acceleratorCardType':'Wrong GPU','acceleratorCardKind':'CPU'})
        with self.assertRaises(RuntimeError):self.cluster.wait_environment('example')

    def test_execute_timeout_keeps_bounded_opt_in_partial_output(self):
        channel=Mock()
        channel.recv_ready.return_value=True
        channel.recv.return_value=b'x'*70000
        channel.recv_stderr_ready.return_value=True
        channel.recv_stderr.return_value=b'diagnostic-only'
        channel.exit_status_ready.return_value=False
        out=Mock(channel=channel);client=Mock()
        client.exec_command.return_value=(Mock(),out,Mock())
        with patch('cluster.time.monotonic',side_effect=[0,100]):
            with self.assertRaises(RemoteExecutionTimeout) as raised:
                self.cluster.execute(client,['/bin/true'],1)
        self.assertEqual(len(raised.exception.stdout),65536)
        self.assertEqual(raised.exception.stderr,'diagnostic-only')
        self.assertNotIn('diagnostic-only',str(raised.exception))
        self.assertIn('--kill-after=5',client.exec_command.call_args.args[0])
        channel.close.assert_called_once()

    def test_generic_gpu_alias_requires_actual_hardware_model(self):
        self.allocated();self.fake_client()
        expected=dict(self.spec,gpus=1,card_type='Example-GPU-32GB')
        self.cluster.registry.update('example',spec=expected)
        self.cluster.environment=Mock(return_value={'userId':'example-owner','wpStatus':'Running','cpu':1,
            'image':expected['image'],'acceleratorCard':1,'acceleratorCardType':'GPU:1','acceleratorCardKind':'GPU'})
        @contextlib.contextmanager
        def ssh(_):yield object()
        self.cluster.ssh=ssh
        self.cluster.execute=Mock(return_value={'exit_code':0,'stdout':'Example GPU-32GB\n','stderr':''})
        record=self.cluster.wait_environment('example')
        self.assertEqual(record['state'],'READY')
        self.assertEqual(record['gpu_verification']['source'],'nvidia-smi')
        self.assertEqual(record['actual_gpu_names'],['Example GPU-32GB'])
        self.cluster.registry.update('example',state='ALLOCATING')
        self.cluster.execute=Mock(return_value={'exit_code':0,'stdout':'Different GPU-80GB\n','stderr':''})
        with self.assertRaisesRegex(RuntimeError,'Physical GPU'):
            self.cluster.wait_environment('example')
        self.assertEqual(self.cluster.registry.get('example')['state'],'ALLOCATING')
        self.cluster.execute=Mock(return_value={'exit_code':0,'stdout':'Example GPU-32GB\nExample GPU-32GB\n','stderr':''})
        with self.assertRaisesRegex(RuntimeError,'Physical GPU'):
            self.cluster.wait_environment('example')
        self.cluster.environment.return_value['acceleratorCardType']='Unknown GPU type'
        self.cluster.execute.reset_mock()
        with self.assertRaisesRegex(RuntimeError,'Allocated GPU model'):
            self.cluster.wait_environment('example')
        self.cluster.execute.assert_not_called()

    def test_pending_stop_waits_until_running_then_pauses_once(self):
        self.allocated();client=self.fake_client()
        self.cluster.environment=Mock(side_effect=[{'userId':'example-owner','wpStatus':status} for status in ('Pending','ImagePulling','Running','Pausing','Pause')])
        with patch('cluster.time.sleep',return_value=None):
            result=self.cluster.stop_environment('example')
        self.assertTrue(result['resource_released'])
        self.assertEqual(client.call.call_count,1);self.assertEqual(client.call.call_args.args[0],'PUT')

    def test_stop_waits_for_asynchronous_release(self):
        self.allocated();client=self.fake_client()
        self.cluster.environment=Mock(side_effect=[{'userId':'example-owner','wpStatus':status} for status in ('Running','Pausing','Pause')])
        with patch('cluster.time.sleep',return_value=None):
            result=self.cluster.stop_environment('example')
        self.assertTrue(result['resource_released']);self.assertEqual(result['final_resource_state'],'Pause')
        self.assertEqual(client.call.call_count,1)
        self.assertEqual(client.call.call_args.args[0],'PUT')

    def test_unconfirmed_stop_never_claims_release_or_deletes(self):
        self.allocated();client=self.fake_client()
        self.cluster.environment=Mock(return_value={'userId':'example-owner','wpStatus':'Pending'})
        with self.assertRaises(TimeoutError):self.cluster.stop_environment('example',timeout=0)
        self.assertFalse(self.cluster.registry.get('example')['resource_released'])
        client.call.assert_not_called()

    def test_watch_reuses_single_ssh_and_sftp_without_collecting(self):
        self.allocated()
        sftp=Mock();client=Mock();client.open_sftp.return_value=sftp
        @contextlib.contextmanager
        def ssh(_):yield client
        self.cluster.ssh=Mock(side_effect=ssh)
        states=iter(['RUNNING','SUCCEEDED'])
        def observe(*args):
            self.assertIs(args[2],sftp)
            return self.cluster.registry.update('example',state=next(states))
        self.cluster.remote_status=observe
        self.cluster.finish=Mock(side_effect=AssertionError('observer collected'))
        self.cluster.stop_environment=Mock(side_effect=AssertionError('observer stopped'))
        with patch('cluster.time.sleep',return_value=None):
            self.assertEqual(self.cluster.watch('example')['state'],'SUCCEEDED')
        self.assertEqual(self.cluster.ssh.call_count,1)
        client.open_sftp.assert_called_once();sftp.close.assert_called_once()

    def test_monitor_network_retries_but_permission_does_not(self):
        self.allocated();attempts=[]
        @contextlib.contextmanager
        def ssh(_):
            attempts.append(1)
            if len(attempts)==1:raise EOFError('simulated')
            yield Mock()
        self.cluster.ssh=ssh
        self.cluster.remote_status=lambda *args:self.cluster.registry.update('example',state='SUCCEEDED')
        with patch('cluster.time.sleep',return_value=None):self.cluster.watch('example')
        self.assertEqual(len(attempts),2)
        @contextlib.contextmanager
        def denied(_):raise PermissionError('simulated');yield
        self.cluster.ssh=denied
        with self.assertRaises(PermissionError):self.cluster.watch('example')

    def test_sftp_inactivity_timeout_reconnects_without_extending_deadline(self):
        self.allocated();clock={'now':0};timeouts=[];connections=[]
        class Channel:
            timeout=None
            def settimeout(self,value):self.timeout=value;timeouts.append(value)
        class SFTP:
            def __init__(self):self.channel=Channel()
            def get_channel(self):return self.channel
            def close(self):pass
        class Client:
            def open_sftp(self):return SFTP()
            def close(self):pass
        @contextlib.contextmanager
        def ssh(_):connections.append(1);yield Client()
        self.cluster.ssh=ssh
        def observe(_run,_client,sftp):
            clock['now']+=sftp.get_channel().timeout
            raise TimeoutError('simulated storage inactivity')
        self.cluster.remote_status=observe
        self.cluster.registry.update('example',spec=dict(self.spec,max_seconds=1))
        with patch('cluster.time.monotonic',side_effect=lambda:clock['now']),patch('cluster.time.sleep',side_effect=lambda seconds:clock.__setitem__('now',clock['now']+seconds)),patch('cluster.random.uniform',return_value=1.1):
            with self.assertRaisesRegex(TimeoutError,'Monitoring deadline'):
                self.cluster.watch('example')
        self.assertEqual(len(connections),2)
        self.assertLessEqual(max(timeouts),30)
        self.assertAlmostEqual(clock['now'],61)
        self.assertLess(timeouts[-1],30)
        self.assertFalse(self.cluster.registry.get('example').get('resource_released',False))

    def test_collection_failure_preserves_compute_and_can_recover(self):
        self.allocated(compute_state='SUCCEEDED',result={'state':'SUCCEEDED'})
        self.cluster.registry.update('example',state='SUCCEEDED')
        @contextlib.contextmanager
        def ssh(_):yield object()
        self.cluster.ssh=ssh
        self.cluster.get=Mock(side_effect=ConnectionError('simulated'))
        with self.assertRaises(ConnectionError):self.cluster.finish('example')
        failed=self.cluster.registry.get('example')
        self.assertEqual(failed['state'],'COLLECTION_FAILED')
        self.assertEqual(failed['compute_state'],'SUCCEEDED')
        self.assertFalse(failed.get('resource_released',False))
        self.cluster.get=Mock(return_value={'sha256':'example'})
        self.cluster.stop_environment=Mock(side_effect=lambda rid:self.cluster.registry.update(rid,resource_released=True))
        recovered=self.cluster.collect('example')
        self.assertEqual(recovered['state'],'SUCCEEDED');self.assertTrue(recovered['collection_complete'])
        self.assertTrue(recovered['resource_released'])

    def test_stale_pid_does_not_reuse_an_unrelated_process(self):
        self.cluster.registry.update('example',supervisor_pid=os.getpid())
        self.cluster.plan=Mock(return_value={})
        process=Mock(pid=424242);process.poll.return_value=0
        with patch('cluster.get_password',return_value='ephemeral-test-value'),patch('cluster.subprocess.Popen',return_value=process) as spawn:
            record=self.cluster.submit(self.spec)
        self.assertEqual(record['supervisor_pid'],424242)
        self.assertNotIn('supervisor_reused',record)
        command=spawn.call_args.args[0]
        self.assertIn('--profile',command);self.assertIn('--non-interactive',command)
        self.assertNotIn('ephemeral-test-value',command)

    def test_ambiguous_launch_never_sends_second_nohup(self):
        self.allocated(launch_attempted=True)
        self.cluster.remote_status=Mock(return_value=self.cluster.registry.get('example'))
        self.cluster.ssh=Mock(side_effect=AssertionError('relaunched'))
        with self.assertRaises(RuntimeError):self.cluster.launch('example')
        self.assertEqual(self.cluster.registry.get('example')['state'],'UNKNOWN')
        self.cluster.ssh.assert_not_called()


    def test_gpu_requires_explicit_card_type(self):
        p=profile();p.pop('card_type')
        with self.assertRaisesRegex(ValueError,'card_type'):
            prepare_spec({'run_id':'gpu','argv':['/bin/true']},p)

    def test_ssh_rejects_wrong_owner_before_credentials(self):
        self.fake_client()
        self.cluster.environment=Mock(return_value={'userId':'different-owner','wpStatus':'Running'})
        self.cluster.ssh_credentials=Mock(side_effect=AssertionError('credentials requested'))
        with self.assertRaises(RuntimeError):
            with self.cluster.ssh('example-env'):pass
        self.cluster.ssh_credentials.assert_not_called()

    def test_completed_stop_preserves_historical_result(self):
        self.allocated(compute_state='SUCCEEDED')
        self.cluster.registry.update('example',state='SUCCEEDED',resource_released=True)
        self.cluster.stop_environment=Mock(side_effect=AssertionError('unnecessary stop'))
        result=self.cluster.cancel('example')
        self.assertEqual(result['state'],'SUCCEEDED')
        self.assertNotIn('cancel_requested',result)
        self.cluster.registry.update('example',resource_released=False)
        self.cluster.stop_environment=Mock(side_effect=lambda rid:self.cluster.registry.update(rid,resource_released=True))
        self.assertEqual(self.cluster.cancel('example')['state'],'SUCCEEDED')

    def test_cancel_waits_for_inflight_allocation_and_stops_it(self):
        client=self.fake_client()
        entered=threading.Event();release=threading.Event();results={};errors=[]
        def allocate(*args,**kwargs):
            entered.set()
            if not release.wait(3):raise TimeoutError('test synchronization')
            return {'wpId':'example-env'}
        client.call.side_effect=allocate
        def create():
            try:results['created']=self.cluster.create_environment('example')
            except BaseException as error:errors.append(error)
        def cancel():
            try:results['cancelled']=self.cluster.cancel('example')
            except BaseException as error:errors.append(error)
        self.cluster.stop_environment=Mock(side_effect=lambda rid:self.cluster.registry.update(rid,resource_released=True))
        creator=threading.Thread(target=create);creator.start()
        self.assertTrue(entered.wait(2))
        canceller=threading.Thread(target=cancel);canceller.start()
        deadline=time.monotonic()+2
        while not self.cluster.registry.get('example').get('cancel_requested') and time.monotonic()<deadline:time.sleep(0.01)
        self.assertTrue(self.cluster.registry.get('example')['cancel_requested'])
        self.assertTrue(canceller.is_alive(),'cancel did not wait for allocation')
        release.set();creator.join(3);canceller.join(3)
        self.assertFalse(creator.is_alive());self.assertFalse(canceller.is_alive())
        self.assertFalse(errors,errors)
        final=self.cluster.registry.get('example')
        self.assertEqual(final['state'],'CANCELLED');self.assertEqual(final['resource_id'],'example-env')
        self.assertTrue(final['resource_released']);self.cluster.stop_environment.assert_called_once_with('example')

    def test_terminal_recovery_collects_before_stopping(self):
        self.allocated(compute_state='SUCCEEDED',remote_runner_pid=123,result={'state':'SUCCEEDED'})
        self.cluster.registry.update('example',state='SUCCEEDED')
        actions=[]
        self.cluster.finish=Mock(side_effect=lambda rid:actions.append('collect'))
        self.cluster.stop_environment=Mock(side_effect=lambda rid:actions.append('stop'))
        self.cluster.run(self.spec)
        self.assertEqual(actions,['collect','stop'])

    def test_collect_running_job_is_rejected(self):
        self.allocated(compute_state='RUNNING')
        self.cluster.remote_status=Mock(return_value=self.cluster.registry.get('example'))
        self.cluster.finish=Mock(side_effect=AssertionError('premature collection'))
        with self.assertRaises(RuntimeError):self.cluster.collect('example')
        self.cluster.finish.assert_not_called()
        del self.cluster.finish
        with self.assertRaises(RuntimeError):self.cluster.finish('example')
        self.assertFalse(self.cluster.registry.get('example').get('collection_complete',False))

    def test_plan_is_offline_and_reports_whole_lifecycle_budget_and_upload_bytes(self):
        script = self.root/'input.py'; script.write_text('print(1)\n')
        spec = prepare_spec({'run_id':'offline','argv':['/bin/true'],'gpus':0,
                             'uploads':[{'local':str(script),'remote':'{run_dir}/input.py'}]},profile())
        self.cluster.resources=Mock(side_effect=AssertionError('offline plan contacted platform'))
        with patch('cluster.get_password',side_effect=AssertionError('offline plan authenticated')):
            plan=self.cluster.plan(spec)
        self.assertFalse(plan['availability_checked'])
        self.assertIsNone(plan['eligible_nodes_now'])
        self.assertEqual(plan['upload_total_bytes'],script.stat().st_size)
        self.assertEqual(plan['time_budget']['total_seconds'],540)
        self.cluster.resources.assert_not_called()

    def test_allocation_refuses_without_live_eligible_node(self):
        client=self.fake_client()
        self.cluster.resources.return_value=[]
        with self.assertRaisesRegex(RuntimeError,'No node'):
            self.cluster.create_environment('example')
        self.cluster.resources.assert_called_once()
        client.call.assert_not_called()
        self.assertEqual(self.cluster.registry.get('example')['state'],'PLANNED')

    def test_training_and_overbudget_jobs_are_rejected_without_execution(self):
        with self.assertRaisesRegex(ValueError,'task-guide'):
            prepare_spec({'run_id':'training','argv':['/bin/true'],'purpose':'training'},profile())
        with self.assertRaisesRegex(ValueError,'Preparation'):
            prepare_spec({'run_id':'too_long','argv':['/bin/true'],'max_seconds':13800,
                          'preparation_seconds':1000},profile())
        with self.assertRaisesRegex(ValueError,'ready_timeout'):
            prepare_spec({'run_id':'ready','argv':['/bin/true'],'preparation_seconds':10},profile())

    def test_recovery_cannot_renew_lease_or_preparation_deadline(self):
        self.allocated()
        self.cluster.registry.update('example',lease_started_epoch=1000,
                                     lease_deadline_epoch=2000,preparation_deadline_epoch=1200)
        with patch('cluster.time.time',return_value=1100),patch('cluster.time.monotonic',return_value=10):
            self.assertEqual(self.cluster._monotonic_deadline('example','lease_deadline_epoch'),910)
        with patch('cluster.time.time',return_value=900),patch('cluster.time.monotonic',return_value=20):
            self.assertEqual(self.cluster._monotonic_deadline('example','lease_deadline_epoch'),910)
            self.assertEqual(self.cluster._ensure_lease('example')['lease_deadline_epoch'],2000)
        with patch('cluster.time.time',return_value=1300),patch('cluster.time.monotonic',return_value=30):
            with self.assertRaisesRegex(TimeoutError,'Preparation'):
                self.cluster.launch('example')

    def test_program_is_not_silently_shortened_to_remaining_lease(self):
        self.allocated()
        now=time.time()
        self.cluster.registry.update('example',lease_deadline_epoch=now+100,
                                     preparation_deadline_epoch=now+90)
        with self.cluster._phase('example','preparation'):
            with self.assertRaisesRegex(TimeoutError,'original program'):
                self.cluster._check_program_budget('example')
        self.assertEqual(self.cluster.registry.get('example')['spec']['max_seconds'],60)

    def test_storage_rejects_unmounted_path_before_any_write(self):
        self.allocated()
        client=object()
        self.cluster.execute=Mock(return_value={'exit_code':0,'stdout':'1 0 0:1 / / rw - overlay overlay rw\n','stderr':''})
        with self.assertRaisesRegex(RuntimeError,'No independent mount'):
            self.cluster._storage_check('example',client)
        self.cluster.execute.assert_called_once_with(client,['cat','/proc/self/mountinfo'],10)

    def test_storage_timeout_reports_stage_and_does_not_start_compute(self):
        self.allocated()
        mounted='1 0 0:1 / / rw - overlay overlay rw\n2 1 0:2 / /data rw - fuse.storage storage rw\n'
        self.cluster.execute=Mock(side_effect=[{'exit_code':0,'stdout':mounted,'stderr':''},
            RemoteExecutionTimeout(b'{"phase":"mkdir"}\n',b'')])
        with self.assertRaisesRegex(RuntimeError,'timed out at mkdir'):
            self.cluster._storage_check('example',object())
        receipt=self.cluster.registry.get('example')['storage_check']
        self.assertFalse(receipt['completed'])
        self.assertEqual(receipt['last_stage'],'mkdir')
        self.assertNotIn('launch_attempted',self.cluster.registry.get('example'))

    def test_collection_deadline_failure_preserves_successful_compute_state(self):
        self.allocated(compute_state='SUCCEEDED',result={'state':'SUCCEEDED'})
        self.cluster.registry.update('example',lease_deadline_epoch=time.time()-1,
                                     preparation_deadline_epoch=time.time()-1)
        self.cluster.ssh=Mock(side_effect=AssertionError('expired collection attempted I/O'))
        with self.assertRaises(TimeoutError):
            self.cluster.finish('example')
        receipt=self.cluster.registry.get('example')
        self.assertEqual(receipt['state'],'COLLECTION_FAILED')
        self.assertEqual(receipt['compute_state'],'SUCCEEDED')
        self.cluster.ssh.assert_not_called()

    def test_explicit_collect_receives_new_window_without_renewing_lease(self):
        self.allocated(compute_state='SUCCEEDED',result={'state':'SUCCEEDED'})
        now=time.time()
        lease=now+600
        self.cluster.registry.update('example',state='COLLECTION_FAILED',
            lease_deadline_epoch=lease,preparation_deadline_epoch=now-100,collection_deadline_epoch=now-1)
        @contextlib.contextmanager
        def ssh(_):yield object()
        self.cluster.ssh=ssh
        self.cluster.get=Mock(return_value={'sha256':'example'})
        self.cluster.stop_environment=Mock(side_effect=lambda rid:self.cluster.registry.update(rid,resource_released=True))
        result=self.cluster.collect('example')
        self.assertEqual(result['state'],'SUCCEEDED')
        self.assertTrue(result['collection_complete'])
        self.assertEqual(result['lease_deadline_epoch'],lease)
        self.assertLessEqual(result['collection_deadline_epoch'],time.time()+120)
        self.assertGreater(result['collection_deadline_epoch'],now)

    def test_expired_lease_still_allows_bounded_stop_confirmation(self):
        self.allocated()
        self.fake_client()
        self.cluster.registry.update('example',lease_deadline_epoch=time.time()-100)
        self.cluster.environment=Mock(return_value={'userId':'example-owner','wpStatus':'Pause'})
        result=self.cluster.stop_environment('example')
        self.assertTrue(result['resource_released'])
        self.assertEqual(result['final_resource_state'],'Pause')

    def test_transfer_reads_refresh_timeout_against_phase_deadline(self):
        clock={'now':0};timeouts=[];reads=[]
        class Channel:
            def settimeout(self,value):timeouts.append(value)
        channel=Channel()
        class Remote:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,_):
                reads.append(clock['now'])
                if len(reads)<=2:
                    clock['now']+=2
                    return b'a'
                return b''
        class SFTP:
            def get_channel(self):return channel
            def open(self,*args):return Remote()
            def close(self):pass
        client=Mock();client.open_sftp.return_value=SFTP()
        expected=hashlib.sha256(b'aa').hexdigest()
        self.cluster.execute=Mock(return_value={'exit_code':0,'stdout':expected+' file\n','stderr':''})
        self.cluster._active_deadline=5
        with patch('cluster.time.monotonic',side_effect=lambda:clock['now']):
            self.cluster.get(client,'/remote/output',self.root/'download')
        self.assertEqual((self.root/'download').read_bytes(),b'aa')
        self.assertEqual(timeouts,[5,5,3,1])

    def test_sftp_negotiation_is_bounded_before_channel_is_available(self):
        closed=threading.Event()
        class Client:
            def close(self):closed.set()
            def open_sftp(self):
                if not closed.wait(2):
                    raise AssertionError('SFTP negotiation was left unbounded')
                raise EOFError('simulated interrupted negotiation')
        self.cluster._active_deadline=time.monotonic()+0.1
        started=time.monotonic()
        with self.assertRaises(EOFError):
            self.cluster._open_sftp(Client())
        self.assertLess(time.monotonic()-started,1)
        self.assertTrue(closed.is_set())

    def test_upload_stops_on_phase_deadline_without_publishing_partial_data(self):
        clock={'now':0};timeouts=[]
        source=self.root/'large-input'
        source.write_bytes(b'x'*(2*1024*1024))
        class Channel:
            def settimeout(self,value):timeouts.append(value)
        class Target:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def set_pipelined(self,_):pass
            def write(self,_):clock['now']+=2
        sftp=Mock()
        sftp.get_channel.return_value=Channel()
        sftp.open.return_value=Target()
        client=Mock();client.open_sftp.return_value=sftp
        self.cluster.execute=Mock(side_effect=AssertionError('Partial upload was published'))
        self.cluster._active_deadline=3
        with patch('cluster.time.monotonic',side_effect=lambda:clock['now']):
            with self.assertRaisesRegex(TimeoutError,'Lifecycle'):
                self.cluster.put(client,source,'/personal/run/input')
        self.cluster.execute.assert_not_called()
        sftp.remove.assert_not_called()
        sftp.close.assert_called_once()
        self.assertEqual(timeouts,[3,3,1])


class RunnerTests(unittest.TestCase):
    def make_run(self,root,argv,seconds):
        (root/'job.json').write_text(json.dumps({'argv':argv,'max_seconds':seconds}))
        return subprocess.Popen([sys.executable,str(SCRIPTS/'remote_runner.py'),str(root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)

    def wait_file(self,path,timeout=5):
        deadline=time.monotonic()+timeout
        while not path.exists() and time.monotonic()<deadline:time.sleep(0.02)
        self.assertTrue(path.exists(),str(path))

    def test_timeout_and_duplicate_runner_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            p=self.make_run(root,[sys.executable,'-c','import time;time.sleep(10)'],1)
            _,stderr=p.communicate(timeout=8)
            self.assertFalse(stderr,stderr.decode(errors='replace'))
            result=json.loads((root/'status.json').read_text())
            self.assertEqual(result['state'],'TIMED_OUT');self.assertEqual(result['exit_code'],124)
            previous=(root/'status.json').read_bytes()
            duplicate=subprocess.run([sys.executable,str(SCRIPTS/'remote_runner.py'),str(root)],capture_output=True,timeout=3)
            self.assertEqual(duplicate.returncode,126)
            self.assertEqual(previous,(root/'status.json').read_bytes())

    def test_watchdog_bounds_compute_even_when_running_status_write_stalls(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'job.json').write_text(json.dumps({'argv':[sys.executable,'-c','import time;time.sleep(20)'],'max_seconds':1}))
            wrapper = """import sys,time,os,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import remote_runner
original=remote_runner.save
def slow(root,state):
    original(root,state)
    if state['state']=='RUNNING':
        time.sleep(2.2)
        alive=True
        try:os.kill(state['pid'],0)
        except ProcessLookupError:alive=False
        (root/'watchdog-proof.json').write_text(json.dumps({'alive_after_stalled_save':alive}))
remote_runner.save=slow
raise SystemExit(remote_runner.main(sys.argv[2]))
"""
            proc=subprocess.run([sys.executable,'-c',wrapper,str(SCRIPTS),str(root)],capture_output=True,text=True,timeout=8)
            self.assertEqual(proc.returncode,1,proc.stderr)
            self.assertFalse(json.loads((root/'watchdog-proof.json').read_text())['alive_after_stalled_save'])
            self.assertEqual(json.loads((root/'status.json').read_text())['state'],'TIMED_OUT')

    def test_remote_status_is_atomically_readable_and_preserves_old_on_write_error(self):
        import remote_runner
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            previous={'state':'RUNNING','value':'previous-complete-snapshot'}
            remote_runner.save(root,previous)
            replace=os.replace
            observed=[]
            def checked_replace(source,destination):
                self.assertEqual(json.loads(Path(source).read_text()),{'state':'SUCCEEDED'})
                self.assertEqual(json.loads(Path(destination).read_text()),previous)
                observed.append(True)
                return replace(source,destination)
            with patch('remote_runner.os.replace',side_effect=checked_replace):
                remote_runner.save(root,{'state':'SUCCEEDED'})
            self.assertEqual(observed,[True])
            self.assertEqual(json.loads((root/'status.json').read_text()),{'state':'SUCCEEDED'})
            def partial_error(_state,handle):
                handle.write('{"state":')
                raise OSError(errno.EIO,'simulated write failure')
            with patch('remote_runner.json.dump',side_effect=partial_error):
                with self.assertRaises(OSError):remote_runner.save(root,{'state':'FAILED'})
            self.assertEqual(json.loads((root/'status.json').read_text()),{'state':'SUCCEEDED'})
            self.assertFalse(list(root.glob('.status-*')))

    def test_sigterm_kills_child_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            worker="import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);Path('descendant.pid').write_text(str(p.pid));time.sleep(30)"
            p=self.make_run(root,[sys.executable,'-c',worker],30)
            try:
                self.wait_file(root/'descendant.pid')
                descendant=int((root/'descendant.pid').read_text())
                p.send_signal(signal.SIGTERM);p.communicate(timeout=8)
                state=json.loads((root/'status.json').read_text())
                self.assertEqual(state['state'],'CANCELLED')
                # A zombie is already dead; an active descendant must not survive cancellation.
                deadline=time.monotonic()+2
                alive=True
                while time.monotonic()<deadline:
                    try:os.kill(descendant,0)
                    except ProcessLookupError:alive=False;break
                    procstat=Path('/proc')/str(descendant)/'stat'
                    if procstat.exists() and procstat.read_text().split()[2]=='Z':alive=False;break
                    time.sleep(0.02)
                self.assertFalse(alive,'Descendant survived runner cancellation')
            finally:
                if p.poll() is None:p.kill();p.communicate()


if __name__=='__main__':unittest.main()
