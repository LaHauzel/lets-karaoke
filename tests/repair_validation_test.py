import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from repair_validation import validate_render


class RenderValidationTests(unittest.TestCase):
    def value(self,first_end=4,second_end=25,gap_hint=True):
        line=dict(raw='hello world',start=1,end=second_end,tokens=[
            dict(text='hello',disp='hello',start=1,end=first_end),
            dict(text='world',disp=' world',start=22,end=second_end)])
        return dict(lines=[line],audio_duration=30,options=dict(gap_hint=gap_hint))

    def test_long_pause_can_pass_when_renderer_splits_and_adds_dots(self):
        result=validate_render(self.value(),[dict(row=1,confidence=.9)],[])
        self.assertTrue(result['gate']);self.assertEqual(result['split_rows'],[1])
        self.assertFalse(validate_render(self.value(gap_hint=False),[dict(row=1,confidence=.9)],[])['gate'])

    def test_split_cannot_hide_flashing_phrase_or_overlong_token(self):
        self.assertFalse(validate_render(self.value(second_end=22.1),[dict(row=1,confidence=.9)],[])['gate'])
        self.assertFalse(validate_render(self.value(first_end=10),[dict(row=1,confidence=.9)],[])['gate'])


if __name__=='__main__':unittest.main()
