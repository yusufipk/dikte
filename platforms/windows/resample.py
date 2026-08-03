"""Turning what a sound card hands over into what whisper wants.

WASAPI in shared mode gives a stream in the device's own format and will not
convert: a microphone runs at 44.1 or 48 kHz, in mono or stereo, and asking it
for 16 kHz mono is refused rather than resampled. So the conversion happens
here, in the same standard library the rest of Dikte is written in.

Rate conversion is an integer accumulator rather than floating-point stepping,
which is the whole point: a float phase accumulated a thousand times a second
for an hour drifts, and drift between the two halves of a meeting is exactly
the thing that must not happen. Adding `dst` per input sample and emitting one
output sample per `src` of credit cannot drift, because the error is carried
rather than rounded away.

Downsampling averages the samples that fall into each output window instead of
picking one of them. It is a crude low-pass, but a real one: point-sampling
48 kHz down to 16 kHz folds everything above 8 kHz back into the speech band,
and a transcription model hears that as noise.
"""

import array


def to_mono(raw, channels):
    """Interleaved s16 bytes -> one array('h') of mono samples."""
    samples = array.array("h")
    usable = len(raw) - (len(raw) % (2 * max(1, channels)))
    if usable <= 0:
        return samples
    samples.frombytes(raw[:usable])
    if channels <= 1:
        return samples
    if channels == 2:
        return array.array("h", [(left + right) // 2
                                 for left, right in zip(samples[0::2],
                                                        samples[1::2])])
    return array.array("h", [
        sum(samples[at:at + channels]) // channels
        for at in range(0, len(samples), channels)
    ])


class Resampler:
    """One channel from `src` Hz to `dst` Hz, keeping its remainder between blocks.

    A stream arrives in blocks of a few hundred samples, and a converter that
    started each block from zero would round the same fraction of a sample off
    a thousand times a second. The credit left over from the last block is what
    makes the whole stream come out at exactly the rate it should.
    """

    def __init__(self, src, dst):
        self.src = max(1, int(src))
        self.dst = max(1, int(dst))
        self._credit = 0
        self._total = 0
        self._count = 0

    @property
    def passthrough(self):
        return self.src == self.dst

    def feed(self, samples):
        """array('h') in, array('h') out."""
        if self.passthrough:
            return samples
        out = array.array("h")
        if self.src > self.dst:
            credit, total, count = self._credit, self._total, self._count
            src, dst = self.src, self.dst
            for sample in samples:
                total += sample
                count += 1
                credit += dst
                if credit >= src:
                    credit -= src
                    # Truncating rather than flooring: floor pulls a quiet
                    # signal half a bit negative, and a constant offset on a
                    # recording is a click at every join.
                    out.append(int(total / count))
                    total = 0
                    count = 0
            self._credit, self._total, self._count = credit, total, count
            return out
        # Upsampling, for the rare device that runs below 16 kHz: hold each
        # sample for as long as it is owed. Nothing worth filtering is being
        # added, so there is nothing to filter.
        credit, src, dst = self._credit, self.src, self.dst
        for sample in samples:
            credit += dst
            while credit >= src:
                credit -= src
                out.append(sample)
        self._credit = credit
        return out


class Converter:
    """A device's blocks turned into 16 kHz mono s16 bytes."""

    def __init__(self, rate, channels, target_rate):
        self.channels = max(1, int(channels))
        self._resampler = Resampler(rate, target_rate)

    def feed(self, raw):
        return self._resampler.feed(to_mono(raw, self.channels)).tobytes()
