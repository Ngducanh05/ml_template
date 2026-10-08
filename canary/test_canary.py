import math
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch
import evaluator
from router import choose_track

class CanaryPolicyTests(unittest.TestCase):
    def healthy(self):
        return dict(up=1, stable_up=1, age=1, stable_age=1, traffic=20,
                    success=20, error_ratio=0, proxy_error_ratio=0, p95=.01)

    def test_kubernetes_upstream_urls_preserve_local_defaults(self):
        import os
        from router import upstream_url
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(upstream_url('stable'), 'http://127.0.0.1:8081')
            self.assertEqual(upstream_url('canary'), 'http://127.0.0.1:8082')
        with patch.dict(os.environ, {'CANARY_STABLE_URL':'http://stable:8000/', 'CANARY_CANARY_URL':'http://canary:8000'}):
            self.assertEqual(upstream_url('stable'), 'http://stable:8000')
            self.assertEqual(upstream_url('canary'), 'http://canary:8000')

    def test_healthy(self):
        self.assertEqual(evaluator.assess(self.healthy()), [])

    def test_bad_metrics_fail_closed(self):
        for key, value in [('up',0),('stable_up',0),('traffic',0),('success',0),
                           ('age',16),('stable_age',16),('p95',.1),
                           ('error_ratio',.05),('proxy_error_ratio',.05),('p95',math.nan)]:
            with self.subTest(key=key, value=value):
                data=self.healthy(); data[key]=value
                self.assertTrue(evaluator.assess(data))
        for key in evaluator.QUERIES:
            data=self.healthy(); del data[key]
            self.assertTrue(evaluator.assess(data))

    def test_missing_duplicate_nonfinite_prometheus_series(self):
        for rows in ([], [{'value':[0,'1']},{'value':[0,'1']}], [{'value':[0,'NaN']}], [{'value':[0,'+Inf']}]):
            with patch.object(evaluator,'api',return_value={'status':'success','data':{'result':rows}}):
                with self.assertRaises(ValueError): evaluator.query('up')

    def test_exact_routing_and_rollback(self):
        for weight in (0,10,25,50,100):
            self.assertEqual(sum(choose_track(i,weight)=='canary' for i in range(1000)),weight*10)

    def test_update_requires_readback(self):
        with patch.object(evaluator,'api',side_effect=[{'canary_weight':0},{'canary_weight':50}]):
            with self.assertRaises(RuntimeError): evaluator.set_weight(0)

    def test_controller_promotes_then_rolls_back_when_target_dies(self):
        state={'canary_weight':10}
        def fake_api(url,data=None):
            if data: state.update(data)
            return dict(state)
        def metric(expr,at=None):
            if expr==evaluator.QUERIES['up'] and state['canary_weight']==100: return 0
            return 1
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'events.jsonl'
            with patch.object(evaluator,'api',side_effect=fake_api), patch.object(evaluator,'query',side_effect=metric), patch.object(evaluator,'observe',return_value=self.healthy()):
                evaluator.run(path,dwell=0,poll=0,duration=5)
            rows=[json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r['weight'] for r in rows],[10,25,50,100,0])
            self.assertEqual(rows[-1]['action'],'ROLLBACK')
            self.assertEqual(state['canary_weight'],0)

    def test_prometheus_outage_causes_rollback(self):
        state={'canary_weight':10}
        def fake_api(url,data=None):
            if data: state.update(data)
            return dict(state)
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(evaluator,'api',side_effect=fake_api), patch.object(evaluator,'query',side_effect=OSError('unavailable')):
                evaluator.run(Path(folder)/'events.jsonl',dwell=0,poll=0,duration=5)
            self.assertEqual(state['canary_weight'],0)


class InfrastructureRegressionTests(unittest.TestCase):
    def test_missing_windows_executable_has_actionable_error(self):
        import bootstrap
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'bin').mkdir()
            (root/'bin'/'grafana').write_text('not a Windows exe')
            with self.assertRaises(RuntimeError) as error: bootstrap.locate('grafana',root)
            for detail in ('URL:', 'Archive:', 'Extracted root:', 'grafana-server.exe', 'grafana.exe'):
                self.assertIn(detail,str(error.exception))

    def test_early_exit_includes_return_code_and_log(self):
        import demo
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            log=Path(folder)/'grafana.log'; log.write_text('SQLite data path missing')
            process=Mock(); process.poll.return_value=1
            with self.assertRaises(RuntimeError) as error:
                demo.wait_for(lambda:False,timeout=30,label='Grafana healthy',processes=[('grafana',process,log)])
            self.assertIn('returncode=1',str(error.exception))
            self.assertIn('SQLite data path missing',str(error.exception))

    def test_pid_reuse_never_selects_unrelated_process(self):
        import demo
        identity=dict(pid=10,parent=1,created='100',executable='python.exe',command='demo service')
        reused=dict(identity,created='200',command='unrelated service')
        child=dict(pid=11,parent=10,created='210',executable='python.exe',command='unrelated child')
        self.assertEqual(demo.owned_tree({'identity':identity,'children':[]},{10:reused,11:child}),{})

    def test_stop_tracks_children_even_if_launcher_exited(self):
        import demo
        parent=dict(pid=10,parent=1,created='100',executable='python.exe',command='demo launcher')
        child=dict(pid=11,parent=10,created='110',executable='python.exe',command='model_service')
        unrelated=dict(pid=12,parent=1,created='120',executable='python.exe',command='other project')
        result=demo.owned_tree({'identity':parent,'children':[child]},{11:child,12:unrelated})
        self.assertEqual(set(result),{11})

    def test_config_creates_sqlite_directory_before_launch(self):
        import demo
        with tempfile.TemporaryDirectory() as folder:
            here=Path(folder)
            (here/'dashboard.json').write_text('{"uid":"local-canary"}')
            (here/'prometheus.yml').write_text('rule_files: [CANARY_ALERT_RULES]')
            with patch.object(demo, 'HERE', here), \
                patch.object(demo, 'RUNTIME', here/'.runtime'), \
                patch.object(demo.bootstrap, 'require_windows', return_value=None), \
                patch.object(
                    demo.bootstrap,
                    'validate_installation',
                    return_value=(here/'server.exe', here)
                ):
                demo.configure()
            for part in ('grafana-data','grafana-logs','prometheus-data','provisioning/datasources','provisioning/dashboards'):
                self.assertTrue((here/'.runtime'/part).is_dir())
            config=(here/'.runtime'/'grafana.ini').read_text()
            self.assertIn((here/'.runtime'/'grafana-data').as_posix(),config)
            self.assertIn('http_addr = 127.0.0.1',config)

if __name__ == '__main__': unittest.main()
