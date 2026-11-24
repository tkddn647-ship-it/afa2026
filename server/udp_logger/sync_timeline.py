from __future__ import annotations


RTP_MODULUS = 1 << 32
RTP_HALF_RANGE = 1 << 31


def rtp_signed_delta(timestamp: int, anchor: int) -> int:
    """Return wrap-safe signed RTP ticks from anchor to timestamp."""
    return ((int(timestamp) - int(anchor) + RTP_HALF_RANGE) % RTP_MODULUS) - RTP_HALF_RANGE


def video_pts_to_pi_mono_ns(
    *,
    video_pts_ns: int,
    pi_rtp_anchor: int,
    pi_mono_anchor_ns: int,
    recording_first_rtp: int,
    rtp_clock_rate: int = 90000,
) -> int:
    if int(rtp_clock_rate) <= 0:
        raise ValueError("rtp_clock_rate는 양수여야 합니다.")
    frame_rtp = (
        int(recording_first_rtp)
        + round(int(video_pts_ns) * int(rtp_clock_rate) / 1_000_000_000)
    ) % RTP_MODULUS
    delta_ticks = rtp_signed_delta(frame_rtp, int(pi_rtp_anchor))
    return int(pi_mono_anchor_ns) + round(
        delta_ticks * 1_000_000_000 / int(rtp_clock_rate)
    )


def rtp_to_ntp_ns(
    *,
    rtp_timestamp: int,
    sender_report_rtp_timestamp: int,
    sender_report_ntp_ns: int,
    rtp_clock_rate: int = 90000,
) -> int:
    if int(rtp_clock_rate) <= 0:
        raise ValueError("rtp_clock_rate는 양수여야 합니다.")
    delta_ticks = rtp_signed_delta(
        int(rtp_timestamp),
        int(sender_report_rtp_timestamp),
    )
    return int(sender_report_ntp_ns) + round(
        delta_ticks * 1_000_000_000 / int(rtp_clock_rate)
    )
