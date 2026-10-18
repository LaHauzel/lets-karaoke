import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from repair_policy import audit,choose_rows,lyric_language,plan_blocks,transcript_anchors


def line(text,start,end):
    return dict(raw=text,start=start,end=end,tokens=[dict(text=text,disp=text,start=start,end=end)])


class RepairPolicyTests(unittest.TestCase):
    def test_language_is_per_line(self):
        self.assertEqual(lyric_language('We can now test this'), 'en')
        self.assertEqual(lyric_language('仮の文章です'), 'ja')
        self.assertEqual(lyric_language('中文','zh'), 'zh')

    def test_high_score_does_not_hide_flash_or_long_highlight(self):
        rows=[line('first phrase',1,1.1),line('another phrase',3,55)]
        report=audit(rows,[dict(row=i,confidence=.99)for i in (1,2)],60)
        self.assertEqual(report['blocking_rows'],[1]);self.assertEqual(report['duration_rows'],[2])

    def test_repeated_phrases_match_distinct_occurrences(self):
        lyrics=['hello world again','a different middle','hello world again']
        baseline=[line(s,i*10,i*10+4)for i,s in enumerate(lyrics)]
        segments=[dict(text=s,start=i*10,end=i*10+4)for i,s in enumerate(lyrics)]
        anchors,_=transcript_anchors(lyrics,segments,baseline)
        self.assertEqual([a['row']for a in anchors],[1,2,3])
        self.assertEqual([a['start']for a in anchors],[0,10,20])

    def test_language_boundary_splits_retry_blocks(self):
        lines=[line('English words',1,2),line('日本語の歌詞',3,4)]
        blocks=plan_blocks(lines,[],[],10)
        self.assertEqual([b['language']for b in blocks],['en','ja'])

    def test_overlapping_audio_routes_do_not_break_a_phrase(self):
        text='hello wonderful world'
        segments=[dict(text='hello wonderful',start=1,end=2,stream='mix'),
                  dict(text='unrelated',start=1.4,end=3,stream='vocals'),
                  dict(text='world',start=2,end=3,stream='mix')]
        anchors,_=transcript_anchors([text],segments,[line(text,1,3)])
        self.assertEqual(len(anchors),1)
        self.assertEqual((anchors[0]['start'],anchors[0]['end']),(1,3))

    def test_lyric_inside_long_asr_sentence_gets_word_boundaries(self):
        text='We test this sample now'
        words=[dict(word=w,start=i,end=i+1)for i,w in enumerate(['unrelated',' words',' We',' test',' this',' sample',' now',' and',' more',' words'])]
        segment=dict(text=''.join(w['word']for w in words),start=0,end=10,words=words)
        anchors,_=transcript_anchors([text],[segment],[line(text,2,7)])
        self.assertEqual((anchors[0]['start'],anchors[0]['end']),(2,7))

    def test_partial_pronoun_anchor_requires_two_models_and_routes(self):
        text='We can now test this'
        words=[dict(word='We',start=0,end=8),dict(word=' can now test this',start=8,end=11)]
        anchors,_=transcript_anchors([text],[dict(text=text,start=0,end=11,words=words)],[line(text,15,15.1)])
        self.assertTrue(anchors[0]['partial_start'])
        option=dict(line=line(text,7.8,11),confidence=.34,model='large',audio='mix')
        other=dict(line=line(text,8,11.2),confidence=.3,model='medium',audio='vocals')
        baseline=[line(text,15,15.1)]
        self.assertEqual(choose_rows(baseline,[],{1:[option]},anchors,20)[0],baseline)
        self.assertNotEqual(choose_rows(baseline,[],{1:[option,other]},anchors,20)[0],baseline)

    def test_model_score_alone_cannot_move_a_line(self):
        old=[line('hello world',1,1.1)]
        options={1:[dict(line=line('hello world',2,4),confidence=.99,model='large',audio='mix')]}
        result,_,decisions=choose_rows(old,[dict(row=1,confidence=.1)],options,[],10)
        self.assertEqual(result,old);self.assertFalse(decisions[0]['accepted'])

    def test_supported_structural_fix_is_accepted_without_approving_it(self):
        old=[line('hello world',1,1.1)]
        options={1:[dict(line=line('hello world',2,4),confidence=.8,model='large',audio='mix')]}
        result,ev,decisions=choose_rows(old,[dict(row=1,confidence=.1)],options,[dict(row=1,start=2,end=4)],10)
        self.assertEqual(result[0]['start'],2);self.assertTrue(decisions[0]['accepted'])
        self.assertNotIn('human_approved',ev[0]);self.assertEqual(old[0]['end'],1.1)

    def test_candidate_cannot_overlap_an_untouched_neighbour(self):
        old=[line('hello world',1,1.1),line('second phrase',4,5)]
        options={1:[dict(line=line('hello world',3,4.5),confidence=.8,model='large',audio='mix')]}
        result,_,decisions=choose_rows(old,[],options,[dict(row=1,start=3,end=4.5)],10)
        self.assertEqual(result,old);self.assertFalse(decisions[0]['accepted'])

    def test_text_loss_and_invalid_tokens_are_rejected(self):
        old=[line('hello world',1,1.1)]
        bad=line('hello world',2,4);bad['tokens'][0]['text']='hello'
        options={1:[dict(line=bad,confidence=.99,model='large',audio='mix')]}
        result,_,_=choose_rows(old,[],options,[dict(row=1,start=2,end=4)],10)
        self.assertEqual(result,old)

    def test_provided_anchor_does_not_drift_to_a_higher_model_score(self):
        old=[line('hello world',2,4)]
        options={1:[dict(line=line('hello world',2.2,4.2),confidence=.99,model='large',audio='mix')]}
        anchors=[dict(row=1,start=2,end=4,source='provided_anchor')]
        result,_,decisions=choose_rows(old,[dict(row=1,confidence=.2)],options,anchors,10)
        self.assertEqual(result,old);self.assertTrue(decisions[0]['protected'])


if __name__=='__main__':unittest.main()
