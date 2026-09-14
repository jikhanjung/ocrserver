import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from astra_cli_bbox import extract, load_captions


class CliBboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / 'figure with spaces.png'
        Image.new('RGB', (200, 100)).save(self.image)
        self.output = self.root / 'bbox.json'
        self.prediction = {'is_compound': True, 'figure_kind': 'fossil_plate', 'notes': [],
                           'panels': [{'label': '27', 'bbox': {'x0': 1, 'y0': 2, 'x1': 999, 'y1': 998},
                                       'caption_indices': [0], 'confidence': 'high'}]}

    def fake_command(self, command, env, cwd, timeout, prompt=None):
        if command[1:3] == ['login', 'status']:
            return 0, '', 'Logged in using ChatGPT'
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('CODEX_API_KEY', env)
        self.assertEqual(command[command.index('--model') + 1], 'gpt-6-astra')
        self.assertIn(str(self.image), command)
        self.assertIn('Target completeness', prompt)
        Path(command[command.index('--output-last-message') + 1]).write_text(json.dumps(self.prediction))
        return 0, '{"type":"turn.completed","usage":{"input_tokens":10}}\n', ''

    def run_extract(self):
        with patch('astra_cli_bbox.shutil.which', return_value='/fake/codex'), \
             patch('astra_cli_bbox.run_command', side_effect=self.fake_command):
            return extract(self.image, 'caption', [{'label': '27', 'text': 'specimen'}], self.output)

    def test_pixel_coordinates_and_provenance(self):
        result = self.run_extract()
        self.assertEqual(result['panels'][0]['bbox'], [0, 0, 200, 100])
        self.assertEqual(result['panels'][0]['caption_indices'], [0])
        self.assertEqual(json.loads((self.root / 'bbox.json.run/run.json').read_text())['status'], 'completed')
        self.assertEqual(result['cli_turn_usage'], [{'input_tokens': 10}])

    def test_invalid_box_does_not_publish_success(self):
        self.prediction['panels'][0]['bbox']['x1'] = 1001
        with self.assertRaises(ValueError):
            self.run_extract()
        self.assertFalse(self.output.exists())
        self.assertEqual(json.loads((self.root / 'bbox.json.run/run.json').read_text())['status'], 'error')

    def test_refuses_overwrite_without_calling_cli(self):
        self.run_extract()
        with patch('astra_cli_bbox.run_command') as call, \
             patch('astra_cli_bbox.shutil.which', return_value='/fake/codex'):
            with self.assertRaisesRegex(ValueError, 'exists'):
                extract(self.image, '', [], self.output)
            call.assert_not_called()

    def test_requires_subscription_login(self):
        with patch('astra_cli_bbox.shutil.which', return_value='/fake/codex'), \
             patch('astra_cli_bbox.run_command', return_value=(0, 'API key login', '')):
            with self.assertRaisesRegex(ValueError, 'ChatGPT login'):
                extract(self.image, '', [], self.output)
        self.assertFalse(self.output.exists())

    def test_timeout_records_failure(self):
        with patch('astra_cli_bbox.shutil.which', return_value='/fake/codex'), \
             patch('astra_cli_bbox.run_command', side_effect=[
                 (0, 'Logged in using ChatGPT', ''), subprocess.TimeoutExpired('codex', 1)]):
            with self.assertRaises(subprocess.TimeoutExpired):
                extract(self.image, '', [], self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(json.loads((self.root / 'bbox.json.run/run.json').read_text())['status'], 'error')

    def test_caption_formats_and_numeric_label_validation(self):
        caption = self.root / 'caption.txt'
        caption.write_text('Full caption')
        sub = self.root / 'sub.json'
        sub.write_text('[{"label":"1","text":"specimen"}]')
        self.assertEqual(load_captions(caption, sub)[1][0]['label'], '1')
        data = self.root / 'caption.json'
        data.write_text('{"caption":"Full caption","subfigures":[{"label":"1","description":"specimen"}]}')
        self.assertEqual(load_captions(data)[0], 'Full caption')
        sub.write_text('[{"label":1}]')
        with self.assertRaises(ValueError):
            load_captions(caption, sub)


if __name__ == '__main__':
    unittest.main()
