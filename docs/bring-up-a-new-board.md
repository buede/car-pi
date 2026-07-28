# Bring up a new board

**For:** anyone who has just built or wired a CAN interface.
**You need:** the board built, and ideally a second CAN node for step 2.
**Time:** about an hour, including a second node.

> **Careful:** work through these in order. Each step narrows down where a fault is. Skipping
> ahead means testing several things at once and learning nothing from a failure.

The goal is to find a mistake on a bench rather than in a car park.

## Step 1: loopback

**Question this answers:** does the controller work, and is the SPI wiring right?

Loopback mode is a property of the interface, not of car-pi, so set it first.

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 loopback on
sudo ip link set can0 up
carpi bench loopback --interface can0
```

You should see frames sent and received, and a pass.

In loopback nothing reaches the transceiver. So a pass here says the controller and its
wiring are good, and says **nothing** about the transceiver end.

If this fails, suspect the crystal frequency first. See [troubleshooting](troubleshooting.md).

## Step 2: two-node bench

**Question this answers:** does the whole stack work over real silicon, at real bit timings?

This is worth the €5 for a second node.

Wire the two interfaces together, CAN_H to CAN_H and CAN_L to CAN_L. Put **a 120 Ω resistor
across the pair at each end**. This is the one place a terminator does belong.

```bash
carpi bench obd  --responder can1 --tester can0
carpi bench tp20 --responder can1 --tester can0
```

You should see both benches pass.

**The `tp20` bench is the most valuable test in this project.** TP2.0 is
connection-oriented, negotiates timing parameters, and needs a keepalive. Both sides of
car-pi's implementation were written from the same specification, with no independently built
implementation to check against. The test suite proves the two agree. Only real controllers at
real bit timings prove the timing is right.

If TP2.0 has a timing bug, this is where it surfaces. On a bench, not in a car park.

## Step 3: listen-only on a car

**Question this answers:** is the car's bus what you think it is?

Listen-only physically cannot transmit, so it cannot disturb the vehicle. Do this before
anything else touches the car.

The procedure is the same pre-flight check every inspection starts with. Follow
[inspect a car](inspect-a-car.md), steps 1 and 2.

You should see steady traffic and no error frames.

## Step 4: scan

**Question this answers:** does the whole thing work?

Only once step 3 is clean, bring the interface up normally:

```bash
sudo systemctl restart carpi-can
carpi scan --channel can0
```

You should see an inspection report.

## If it did not work

See [troubleshooting](troubleshooting.md), which lists these symptoms in the order worth
checking.

## Next

- Board not built yet? → [build the CAN interface](build-the-can-interface.md)
- Working? → [inspect a car](inspect-a-car.md)

## Words used here

Loopback, transceiver, terminator, bitrate, TP2.0 — see the [glossary](glossary.md).
