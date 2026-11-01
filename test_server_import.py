from pathlib import Path
import hashlib
import tempfile
import unittest
from server_import import resolve_reference


class ImportTests(unittest.TestCase):
    def test_explicit_reference_mapping_preserves_original_and_checks_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'audio.wav').write_bytes(b'original WAV bytes');digest=hashlib.sha256((root/'audio.wav').read_bytes()).hexdigest()
            proof=resolve_reference('/Users/old/Smartgram/audio.wav',{'/Users/old/Smartgram':str(root)},digest)
            self.assertEqual(proof['original_path'],'/Users/old/Smartgram/audio.wav');self.assertEqual(proof['sha256'],digest)
            with self.assertRaises(ValueError):resolve_reference(proof['original_path'],{'/Users/old/Smartgram':str(root)},'wrong')

    def test_traversal_and_unmapped_paths_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            for value in ['/different/path','/Users/old/Smartgram/../private','relative/file']:
                with self.assertRaises(ValueError):resolve_reference(value,{'/Users/old/Smartgram':temp})


if __name__=='__main__':unittest.main()
