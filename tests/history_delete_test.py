import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from local_history import delete, identify

class HistoryDeleteTest(unittest.TestCase):
    def test_selected_scope_and_active_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); a=root/'webui'/'a'; b=root/'nested'/'b'; a.mkdir(parents=True); b.mkdir(parents=True)
            ia,ib=identify(a,root),identify(b,root)
            self.assertEqual(delete(root,[ia],root/'webui'),[ia]); self.assertFalse(a.exists()); self.assertTrue(b.exists())
            with self.assertRaises(ValueError): delete(root,[ib],root/'webui',[ib])
            with self.assertRaises(ValueError): delete(root,['../../'],root/'webui')

if __name__=='__main__': unittest.main()
