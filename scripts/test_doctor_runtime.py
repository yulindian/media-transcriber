import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock,patch
import transcribe


class DoctorRuntimeTests(unittest.TestCase):
    def test_ffprobe_is_required_even_when_ffmpeg_and_models_exist(self):
        cfg={'runtime':'unused','output':'unused','anonymous_api':{'enabled':False}}
        with patch('pathlib.Path.is_file',return_value=True),patch('pathlib.Path.stat',return_value=Mock(st_size=1)),patch('transcribe.shutil.which',side_effect=lambda name:'ffmpeg' if name=='ffmpeg' else None):
            result=transcribe.doctor(cfg)
            self.assertEqual(result['status'],'needs_setup')
            self.assertIn('ffprobe',result['missing'])

    def test_relative_config_paths_resolve_from_skill_not_callers_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'任意 Agent 安装';root.mkdir()
            (root/'config.example.json').write_text('{"runtime":"runtime","output":"outputs"}',encoding='utf-8')
            with patch.object(transcribe,'ROOT',root):cfg=transcribe.config()
            self.assertEqual(Path(cfg['runtime']),root/'runtime')
            self.assertEqual(Path(cfg['output']),root/'outputs')
    def test_enabled_anonymous_transport_requires_curl(self):
        cfg={'runtime':'unused','output':'unused','anonymous_api':{'enabled':True}}
        with patch('pathlib.Path.is_file',return_value=True),patch('pathlib.Path.stat',return_value=Mock(st_size=1)),patch('transcribe.shutil.which',side_effect=lambda name:'ffmpeg' if name=='ffmpeg' else None):
            self.assertEqual(transcribe.doctor(cfg)['status'],'needs_setup')


if __name__=='__main__':unittest.main()
