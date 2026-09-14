import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from astra_panels import (MODEL, atomic_json, build_review, decode_response,
                                 hash_file, make_request, process_one, validate_prediction)


class AstraPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        (self.bundle / 'images').mkdir(parents=True)
        self.output = self.root / 'output'
        for sub in ('raw', 'predictions', 'crops'):
            (self.output / sub).mkdir(parents=True)
        path = self.bundle / 'images/ref_1_fig_2.png'
        image = Image.new('RGB', (200, 100), 'red')
        image.paste('blue', (100, 0, 200, 100))
        image.save(path)
        self.item = {'stem': 'ref_1_fig_2', 'figure_id': 2, 'reference_id': 1,
                     'figure_number': 'Fig. 1', 'page_no': 1, 'width': 200, 'height': 100,
                     'image': 'images/ref_1_fig_2.png', 'image_sha256': hash_file(path),
                     'caption': 'Two fossil specimens',
                     'subfigures': [{'label': '2a', 'description': '<script>left fossil</script>'},
                                    {'label': '2b', 'description': 'Right fossil'}]}
        self.prediction = {'is_compound': True, 'figure_kind': 'fossil_plate', 'notes': [],
                           'panels': [
                               {'label': '2a', 'bbox': {'x0': 0, 'y0': 0, 'x1': 500, 'y1': 1000},
                                'caption_indices': [0], 'confidence': 'high'},
                               {'label': '2b', 'bbox': {'x0': 500, 'y0': 0, 'x1': 1000, 'y1': 1000},
                                'caption_indices': [1], 'confidence': 'high'}]}
        self.response = {'status': 'completed', 'model': MODEL, 'usage': {'input_tokens': 100},
                         'output': [{'type': 'reasoning'}, {'type': 'message', 'content': [
                             {'type': 'output_text', 'text': json.dumps(self.prediction)}]}]}

    def process(self):
        return process_one(self.bundle, self.output, self.item, 'high', 'test-placeholder',
                           threading.Event(), False)

    def test_prompt_preserves_source_captions_and_target_model(self):
        request = make_request(self.bundle, self.item, 'high')
        self.assertEqual(request['model'], 'gpt-6-astra')
        metadata = json.loads(request['input'][0]['content'][0]['text'])
        self.assertEqual(metadata['existing_subfigures'], self.item['subfigures'])
        self.assertNotIn('temperature', request)
        self.assertTrue(request['text']['format']['strict'])

    def test_numeric_labels_exact_crops_and_original_captions(self):
        with patch('astra_panels.call_api', return_value=self.response):
            self.process()
        summary = build_review(self.bundle, self.output, {'figures': [self.item]})
        self.assertEqual(summary['panels'], 2)
        result = json.loads((self.output / 'panels.json').read_text())
        panel = result['figures'][0]['panels'][1]
        self.assertEqual(panel['label'], '2b')
        self.assertEqual(panel['existing_captions'][0]['description'], 'Right fossil')
        with Image.open(self.output / panel['file']) as image:
            self.assertEqual(image.size, (100, 100))
            self.assertEqual(image.getpixel((50, 50)), (0, 0, 255))
        self.assertNotIn('<script>', (self.output / 'index.html').read_text())

    def test_resume_does_not_call_api_again(self):
        with patch('astra_panels.call_api', return_value=self.response) as api:
            self.process()
            self.process()
        self.assertEqual(api.call_count, 1)

    def test_incomplete_response_not_marked_complete(self):
        self.response['status'] = 'incomplete'
        with patch('astra_panels.call_api', return_value=self.response):
            self.assertEqual(self.process()['status'], 'error')
        self.assertTrue((self.output / 'raw/ref_1_fig_2.json').exists())

    def test_refusal_not_treated_as_empty_panel_list(self):
        self.response['output'] = [{'type': 'message', 'content': [{'type': 'refusal'}]}]
        with self.assertRaisesRegex(ValueError, 'refused'):
            decode_response(self.response)

    def test_invalid_box_and_caption_index_rejected(self):
        for mutation in ('nan', 'negative', 'inverted', 'caption'):
            prediction = copy.deepcopy(self.prediction)
            panel = prediction['panels'][0]
            if mutation == 'nan': panel['bbox']['x0'] = float('nan')
            if mutation == 'negative': panel['bbox']['x0'] = -1
            if mutation == 'inverted': panel['bbox']['x0'] = 600
            if mutation == 'caption': panel['caption_indices'] = [99]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_prediction(prediction, self.item)

    def test_single_figure_remains_one_full_image(self):
        prediction = copy.deepcopy(self.prediction)
        prediction['is_compound'] = False
        prediction['panels'] = [prediction['panels'][0]]
        prediction['panels'][0]['bbox']['x1'] = 1000
        self.assertEqual(len(validate_prediction(prediction, self.item)['panels']), 1)

    def test_changed_source_is_rejected_before_api(self):
        Image.new('RGB', (200, 100)).save(self.bundle / self.item['image'])
        with patch('astra_panels.call_api') as api:
            self.assertEqual(self.process()['status'], 'error')
            api.assert_not_called()

    def test_unprocessed_figures_remain_pending(self):
        summary = build_review(self.bundle, self.output, {'figures': [self.item]})
        self.assertEqual(summary['pending'], 1)
        self.assertEqual(summary['completed'], 0)

    def test_more_than_26_panels_allowed(self):
        prediction = copy.deepcopy(self.prediction)
        prediction['panels'] *= 20
        self.assertEqual(len(validate_prediction(prediction, self.item)['panels']), 40)


if __name__ == '__main__':
    unittest.main()
