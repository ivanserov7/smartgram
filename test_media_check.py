"""Контракты проверки локального монтажа; сетевые API и настоящий FFmpeg не вызываются."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_check import check_media, create_sound, create_subtitles, validate_probe
from media_tools import ENCODERS, FILTERS, MediaError, load_media_settings, require_cyrillic, require_features


class MediaCheckTests(unittest.TestCase):
    def test_all_required_features_and_missing_libass(self):
        filters = '\n'.join(f' ... {name} V->V' for name in FILTERS)
        encoders = '\n'.join(f' V..... {name} description' for name in ENCODERS)
        require_features(filters, encoders)
        with self.assertRaisesRegex(MediaError, 'фильтр ass'):
            require_features(filters.replace(' ... ass V->V', ''), encoders)
        with self.assertRaisesRegex(MediaError, 'кодер libx264'):
            require_features(filters, ' V..... other libx264 is only a description')

    def test_font_must_include_yo_not_only_basic_russian(self):
        require_cyrillic('20-7e 400-52f')
        with self.assertRaisesRegex(MediaError, 'ё'):
            require_cyrillic('20-7e 410-44f')

    def test_probe_rejects_wrong_size_fps_codec_and_missing_frames(self):
        probe = {'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 1080, 'height': 1920,
                              'pix_fmt': 'yuv420p', 'avg_frame_rate': '30/1', 'nb_read_frames': '150'},
                             {'codec_type': 'audio', 'codec_name': 'aac', 'sample_rate': '48000'}],
                 'format': {'duration': '5.000'}}
        validate_probe(probe)
        for name, value in (('width', 720), ('avg_frame_rate', '25/1'), ('codec_name', 'hevc'), ('nb_read_frames', '149')):
            bad = copy.deepcopy(probe)
            bad['streams'][0][name] = value
            with self.subTest(name=name), self.assertRaises(MediaError):
                validate_probe(bad)

    def test_missing_tools_saved_as_blocked_without_fake_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('media_check.load_media_settings', side_effect=MediaError('Homebrew требует пароль.')):
                result = check_media(root)
            self.assertEqual(result['status'], 'blocked')
            self.assertFalse((root/'test_vertical.mp4').exists())
            self.assertNotIn('full_decode_passed', result)
            self.assertEqual(json.loads((root/'validation.json').read_text())['api_requests'], 0)
            self.assertIn('Homebrew требует пароль.', (root/'report.html').read_text())

    def test_synthetic_sound_and_utf8_subtitles_without_voice_or_network(self):
        import wave
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_sound(root/'sound.wav')
            with wave.open(str(root/'sound.wav')) as audio:
                self.assertEqual(audio.getnframes()/audio.getframerate(), 5)
                self.assertEqual(audio.getnchannels(), 1)
            create_subtitles(root, 'Arial')
            self.assertIn('ёж, спутник, Земля', (root/'subtitles.ass').read_text())
            self.assertIn('Проверка субтитров: ёж, спутник, Земля', (root/'subtitles.srt').read_text())

    def test_explicit_missing_path_does_not_silently_use_other_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory)/'.env'
            env.write_text('FFMPEG_PATH=/nonexistent/ffmpeg\nFFPROBE_PATH=/nonexistent/ffprobe\n')
            with patch.dict('os.environ', {}, clear=True), self.assertRaisesRegex(MediaError, 'Исполняемый файл недоступен'):
                load_media_settings(env)


if __name__ == '__main__':
    unittest.main()
