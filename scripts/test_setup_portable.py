import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('portable_setup',Path(__file__).with_name('setup.py'))
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)


class PortableSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'其他 Agent 中文 空间';self.root.mkdir()
        (self.root/'config.example.json').write_text(json.dumps({'runtime':'runtime','output':'outputs','threads':4}),encoding='utf-8')
        (self.root/'requirements.txt').write_text('example==1.0\n',encoding='utf-8')
        self.args=argparse.Namespace(runtime=None,output=None,plan=False)

    def test_plan_creates_no_files_and_uses_skill_local_paths(self):
        before=list(self.root.rglob('*'))
        result=setup.install(self.args,self.root,execute=False)
        self.assertEqual(list(self.root.rglob('*')),before)
        self.assertEqual(result['config']['runtime'],str(self.root/'runtime'))
        self.assertIn(str(self.root/'requirements.txt'),result['commands'][1])

    def test_existing_runtime_output_and_settings_are_preserved(self):
        cfg={'runtime':'my-runtime','output':'my-outputs','threads':2,'anonymous_api':{'enabled':False}}
        (self.root/'config.json').write_text(json.dumps(cfg),encoding='utf-8')
        result=setup.install(self.args,self.root,execute=False)
        self.assertEqual(result['config']['threads'],2)
        self.assertFalse(result['config']['anonymous_api']['enabled'])
        self.assertEqual(result['config']['runtime'],str(self.root/'my-runtime'))

    def test_missing_system_dependency_stops_before_installing_or_writing(self):
        with patch.object(setup.shutil,'which',return_value=None),patch.object(setup.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'uv'):setup.install(self.args,self.root)
            run.assert_not_called()
            self.assertFalse((self.root/'config.json').exists())

    def test_dependency_failure_does_not_publish_config(self):
        import subprocess
        with patch.object(setup.shutil,'which',side_effect=lambda n:n),patch.object(setup.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['uv'])):
            with self.assertRaises(subprocess.CalledProcessError):setup.install(self.args,self.root)
        self.assertFalse((self.root/'config.json').exists())

    def test_success_uses_requirements_and_requires_ready_doctor_status(self):
        import subprocess
        for status in ('ready','needs_setup'):
            with self.subTest(status=status),patch.object(setup.shutil,'which',side_effect=lambda n:n),patch.object(setup.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=json.dumps({'status':status,'missing':['ffprobe'] if status!='ready' else []}),stderr='')) as run:
                if status=='ready':
                    result=setup.install(self.args,self.root)
                    self.assertEqual(result['status'],'installed')
                    self.assertIn('-r',run.call_args_list[1].args[0])
                else:
                    with self.assertRaisesRegex(ValueError,'not ready'):setup.install(self.args,self.root)

    def test_explicit_override_does_not_change_other_personal_settings(self):
        (self.root/'config.json').write_text(json.dumps({'runtime':'existing','output':'old-output','threads':2}),encoding='utf-8')
        self.args.output='new-output'
        cfg=setup.install(self.args,self.root,execute=False)['config']
        self.assertEqual(cfg['runtime'],str(self.root/'existing'))
        self.assertEqual(cfg['output'],str(self.root/'new-output'))
        self.assertEqual(cfg['threads'],2)


if __name__=='__main__':unittest.main()
