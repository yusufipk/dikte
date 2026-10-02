"""Home/Settings Spaces policy must not reuse the indicator's AppKit policy."""
import unittest
from unittest import mock

from dikte import mac_window


class PresentationPolicy(unittest.TestCase):
    def api(self, behavior=0, window=42):
        api = mock.Mock()
        api.selector.side_effect = lambda name: name
        api.ask.return_value = window
        api.ask_unsigned.return_value = behavior
        return api

    def apply(self, api, platform='cocoa'):
        with mock.patch.object(mac_window.QGuiApplication, 'platformName',
                               return_value=platform), \
                mock.patch.object(mac_window, '_appkit', return_value=api) as load:
            answer = mac_window.move_to_active_space(mock.Mock(winId=lambda: 7))
        return answer, load

    def test_preserves_unrelated_flags_and_replaces_all_spaces_policy(self):
        other = mac_window.FULL_SCREEN_AUXILIARY | mac_window.IGNORES_CYCLE
        api = self.api(other | mac_window.CAN_JOIN_ALL_SPACES)
        self.assertTrue(self.apply(api)[0])
        self.assertEqual(api.tell_unsigned.call_args.args[1:],
                         (b'setCollectionBehavior:', other | mac_window.MOVE_TO_ACTIVE_SPACE))
        api.tell_bool.assert_not_called()

    def test_repeated_presentation_does_not_rewrite_identical_policy(self):
        api = self.api(mac_window.MOVE_TO_ACTIVE_SPACE)
        self.assertTrue(self.apply(api)[0])
        api.tell_unsigned.assert_not_called()

    def test_offscreen_never_loads_or_messages_objc(self):
        api = self.api()
        answer, load = self.apply(api, 'offscreen')
        self.assertFalse(answer)
        load.assert_not_called()
        api.ask.assert_not_called()

    def test_missing_native_window_is_left_alone(self):
        api = self.api(window=0)
        self.assertFalse(self.apply(api)[0])
        api.ask_unsigned.assert_not_called()
        api.tell_unsigned.assert_not_called()

    def test_unavailable_runtime_is_nonfatal(self):
        api = self.api()
        api.ask.side_effect = OSError('unavailable')
        self.assertFalse(self.apply(api)[0])
