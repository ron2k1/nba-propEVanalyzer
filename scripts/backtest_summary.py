#!/usr/bin/env python3
"""Parse backtest JSON from stdin or path and print summary (sample, coverage, roiReal, bins)."""
import json
import sys

def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            r = json.load(f)
    else:
        r = json.load(sys.stdin)

    rep = r.get("reports", {}).get("full") or r
    if not rep:
        print("No 'reports.full' in JSON")
        return

    print("sampleCount:", rep.get("sampleCount"))
    print("realLineSamples:", rep.get("realLineSamples"))
    print("missingLineSamples:", rep.get("missingLineSamples"))
    real = rep.get("realLineSamples") or 0
    miss = rep.get("missingLineSamples") or 0
    if real + miss > 0:
        print(f"coverage: {real / (real + miss) * 100:.1f}%")
    print()

    roi = rep.get("roiReal") or {}
    print("roiReal bets:", roi.get("betsPlaced"))
    print("roiReal hitRate:", roi.get("hitRatePct"))
    print("roiReal ROI:", roi.get("roiPctPerBet"))
    print("roiReal W/L:", roi.get("wins"), "/", roi.get("losses"))
    print()
    print("roiSynth ROI:", (rep.get("roiSynth") or {}).get("roiPctPerBet"))
    print()

    print("Per-stat real-line:")
    for stat, d in (rep.get("realLineStatRoi") or {}).items():
        if d.get("betsPlaced", 0) > 0:
            print(f"  {stat}: {d['betsPlaced']} bets | {d['hitRatePct']}% hit | {d.get('roiPctPerBet'):+.3f}% ROI")
    print()

    print("Calib bins:")
    for b in rep.get("realLineCalibBins") or []:
        n = b.get("betsPlaced", 0)
        if n > 0:
            print(f"  {b['bin']}: {n} bets | {b['hitRatePct']}% hit | {b.get('roiPctPerBet', 0):+.3f}% ROI")
    print()

    print("errors:", rep.get("projectionErrors"))
    policy = r.get("bettingPolicy") or {}
    print("policy whitelist:", policy.get("statWhitelist"))
    print("policy blocked bins:", policy.get("blockedProbBins"))

if __name__ == "__main__":
    main()
