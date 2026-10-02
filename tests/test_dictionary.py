"""Synthetic glossary migration, learning and offscreen record management."""
from PyQt6.QtWidgets import QApplication
from dikte import config as cfg, dictionary
from dikte.dictionary_ui import DictionaryEditor
from tests.support import DikteTest

_app = QApplication.instance() or QApplication([])


class Dictionary(DikteTest):
    def test_lossless_migration_reload_and_legacy_edit(self):
        text = '  First name, second term\n\nİpek: preferred spelling  \n'
        conf = self.config(transcribe_prompt=text)
        self.assertEqual(dictionary.render(conf.dictionary_entries()), text)
        conf.save()
        again = cfg.Config()
        self.assertEqual(again['transcribe_prompt'], text)
        self.assertEqual(again.dictionary_entries(), conf.dictionary_entries())
        again['transcribe_prompt'] = 'Legacy CLI edit\n'
        again.save()
        self.assertEqual(dictionary.render(cfg.Config().dictionary_entries()),
                         'Legacy CLI edit\n')

    def test_unicode_dedup_and_expressions(self):
        rows = dictionary.add([], 'İpek Işık')
        with self.assertRaises(ValueError):
            dictionary.add(rows, 'i\u0307pek ışık'.replace('i\u0307', 'i'))
        with self.assertRaises(ValueError):
            dictionary.add(dictionary.add([], 'IŞIK'), 'ışık')
        self.assertEqual(dictionary.key('İ'), dictionary.key('I\u0307'))
        rows = dictionary.add(rows, 'Başka terim', replace_id=rows[0]['id'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['text'], 'Başka terim')

    def test_learning_scope_and_persistence(self):
        conf = self.config()
        raw = 'we tested Aster and compared Aster today.'
        conf.learn_dictionary(raw, raw)
        self.assertEqual(conf.dictionary_entries(), [])
        conf['dictionary_learning'] = True
        conf.learn_dictionary(raw, raw)
        self.assertEqual(conf.dictionary_entries()[0]['source'], 'auto')
        conf.learn_dictionary(raw, raw)
        self.assertEqual(len(conf.dictionary_entries()), 1)
        self.assertIn('Aster', conf.cleanup_prompt())
        self.assertEqual(cfg.Config()['transcribe_prompt'], 'Aster')
        self.assertEqual(dictionary.candidates(raw, 'removed'), [])
        self.assertEqual(dictionary.candidates('Aster goes. Aster comes.', raw), [])
        self.assertEqual(dictionary.candidates('x AAA AAA Aaabbb Aaabbb 123 123', raw), [])

    def test_editor_crud_filters_cancel_reset_and_merge(self):
        conf = self.config(transcribe_prompt='  Legacy\n')
        ui = DictionaryEditor()
        self.addCleanup(ui.deleteLater)
        ui.load(conf)
        ui.text.setPlainText('Multiword expression')
        ui.add()
        self.assertEqual(len(conf.dictionary_entries()), 1)  # staged
        ui.save(conf)
        self.assertEqual(len(conf.dictionary_entries()), 2)
        conf['dictionary_learning'] = True
        raw = 'we tested Aster and compared Aster today.'
        conf.learn_dictionary(raw, raw)
        ui.save(conf)  # preserve learning since load
        self.assertEqual(len(conf.dictionary_entries()), 3)
        ui.load(conf)
        ui.source.setCurrentIndex(1)
        self.assertEqual(ui.list.count(), 1)
        ui.list.setCurrentRow(0)
        ui.text.setPlainText('Aster preferred')
        ui.edit()  # user correction becomes manual
        self.assertEqual(ui.list.count(), 0)
        ui.source.setCurrentIndex(2)
        ui.search.setText('preferred')
        self.assertEqual(ui.list.count(), 1)
        ui.list.setCurrentRow(0)
        ui.delete()
        ui.save(conf)
        self.assertEqual(len(conf.dictionary_entries()), 2)
        conf['dictionary_learning'] = True
        conf.learn_dictionary(raw, raw)
        ui.reset()
        ui.learning.setChecked(False)
        ui.save(conf)
        self.assertTrue(all(r['source'] == 'manual' for r in conf.dictionary_entries()))
        self.assertFalse(conf['dictionary_learning'])
        ui.legacy.setPlainText('  Replacement\n')
        ui.save(conf)
        self.assertEqual(conf['transcribe_prompt'], '  Replacement\n')

    def test_save_retry_does_not_resurrect_deleted_entry(self):
        conf = self.config(transcribe_prompt='Original')
        ui = DictionaryEditor()
        self.addCleanup(ui.deleteLater)
        ui.load(conf)
        before = dict(conf.data)
        ui.list.setCurrentRow(0)
        ui.delete()
        ui.save(conf)
        conf.data = before  # SettingsWindow rollback after disk failure
        ui.save(conf)
        self.assertEqual(conf['transcribe_prompt'], '')

    def test_external_legacy_replacement_is_not_merged_with_stale_rows(self):
        conf = self.config(transcribe_prompt='Original')
        ui = DictionaryEditor()
        self.addCleanup(ui.deleteLater)
        ui.load(conf)
        conf['transcribe_prompt'] = 'Legacy replacement'
        ui.save(conf)
        self.assertEqual(conf['transcribe_prompt'], 'Legacy replacement')

    def test_invalid_record_ids_fall_back_losslessly(self):
        for rows in ([{'id': '', 'text': 'one', 'source': 'manual'}],
                     [{'id': 'same', 'text': text, 'source': 'manual'}
                      for text in ('one', 'two')]):
            text = dictionary.render(rows)
            result = dictionary.reconcile(rows, text)
            self.assertEqual(dictionary.render(result), text)
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0]['id'])

    def test_manual_edit_and_add_win_concurrent_learning_collisions(self):
        for edit in (True, False):
            conf = self.config(transcribe_prompt='Old')
            ui = DictionaryEditor()
            self.addCleanup(ui.deleteLater)
            ui.load(conf)
            if edit:
                ui.list.setCurrentRow(0)
            ui.text.setPlainText('New')
            ui.edit() if edit else ui.add()
            conf.set_dictionary(dictionary.add(conf.dictionary_entries(), 'New', 'auto'))
            ui.save(conf)
            matches = [r for r in conf.dictionary_entries() if r['text'] == 'New']
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]['source'], 'manual')
