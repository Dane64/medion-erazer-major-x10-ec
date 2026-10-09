import subprocess
import tempfile
import unittest
from pathlib import Path

from medion_fan_control.audio import AlsaMixer, AudioError, AudioPowerBackend, parse_amixer_contents
from medion_fan_control.sysfs import Sysfs

from tests.fakesys import read, write

CONTENTS = """numid=2,iface=MIXER,name='Master Playback Volume'
  ; type=INTEGER,access=rw---R--,values=2,min=0,max=87,step=0
  : values=60,60
  | dBscale-min=-65.25dB,step=0.75dB,mute=0
numid=1,iface=MIXER,name='Master Playback Switch'
  ; type=BOOLEAN,access=rw------,values=1
  : values=on
numid=13,iface=MIXER,name='Auto-Mute Mode'
  ; type=ENUMERATED,access=rw------,values=1,items=2
  ; Item #0 'Disabled'
  ; Item #1 'Enabled'
  : values=1
numid=20,iface=CARD,name='Headphone Jack'
  ; type=BOOLEAN,access=r-------,values=1
  : values=off
numid=21,iface=PCM,name='Playback Channel Map'
  ; type=INTEGER,access=r----R--,values=2,min=0,max=36,step=0
  : values=0,0
"""


class AudioTests(unittest.TestCase):
    def test_parses_controls(self):
        controls = {c.name: c for c in parse_amixer_contents(CONTENTS)}
        self.assertEqual(controls["Master Playback Volume"].maximum, 87)
        self.assertEqual(controls["Master Playback Volume"].values, ["60", "60"])
        self.assertEqual(controls["Auto-Mute Mode"].items, ["Disabled", "Enabled"])
        self.assertFalse(controls["Headphone Jack"].writable)

    def test_lists_only_writable_mixer_controls_and_sets_all_channels(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, CONTENTS, "")

        mixer = AlsaMixer(runner, which=lambda name: "/usr/bin/amixer")
        controls = mixer.controls(0)
        self.assertEqual(sorted(c.name for c in controls),
                         ["Auto-Mute Mode", "Master Playback Switch", "Master Playback Volume"])
        volume = next(c for c in controls if c.type == "INTEGER")
        mixer.set(0, volume, 40)
        self.assertEqual(calls[-1], ["amixer", "-c", "0", "-q", "cset", "numid=2", "40,40"])
        with self.assertRaises(AudioError):
            mixer.set(0, volume, 99)

    def test_hda_power_save(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write(root, "sys/module/snd_hda_intel/parameters/power_save", 1)
        write(root, "sys/module/snd_hda_intel/parameters/power_save_controller", "Y")
        backend = AudioPowerBackend(Sysfs(root))
        self.assertEqual(backend.get(), {"available": True, "power_save_s": 1, "power_save_controller": True})
        backend.set({"power_save_s": 0, "power_save_controller": False})
        self.assertEqual(read(root, "sys/module/snd_hda_intel/parameters/power_save"), "0")
        self.assertEqual(read(root, "sys/module/snd_hda_intel/parameters/power_save_controller"), "N")
        with self.assertRaises(AudioError):
            backend.set({"power_save_s": -1})


if __name__ == "__main__":
    unittest.main()
