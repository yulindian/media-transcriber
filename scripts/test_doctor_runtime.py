import unittest
from unittest.mock import Mock,patch
import transcribe


class DoctorRuntimeTests(unittest.TestCase):
    def test_enabled_anonymous_transport_requires_curl(self):
        cfg={'runtime':'unused','output':'unused','anonymous_api':{'enabled':True}}
        with patch('pathlib.Path.is_file',return_value=True),patch('pathlib.Path.stat',return_value=Mock(st_size=1)),patch('transcribe.shutil.which',side_effect=lambda name:'ffmpeg' if name=='ffmpeg' else None):
            self.assertEqual(transcribe.doctor(cfg)['status'],'needs_setup')


if __name__=='__main__':unittest.main()
