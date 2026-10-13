const assert = require("node:assert/strict");
const test = require("node:test");
const { groupReviewItems, buildMotionSlots } = require("../frigate/black/courtyard-review/app.js");

const item = (camera, start, end, severity = "alert", label = "person") => ({
  id: `${camera}-${start}`, camera, start_time: start, end_time: end,
  severity, data: { objects: [label] },
});

test("overlapping courtyard angles form one group and preserve both source records", () => {
  const grouped = groupReviewItems([
    item("common_camera02", 105, 120), item("common_camera01", 100, 110),
  ]);
  assert.equal(grouped.length, 1);
  assert.deepEqual(grouped[0].items.map((entry) => entry.camera), ["common_camera01", "common_camera02"]);
});

test("different object types, severities, distant times and non-courtyard cameras stay apart", () => {
  const grouped = groupReviewItems([
    item("common_camera01", 100, 110),
    item("common_camera02", 105, 115, "detection", "car"),
    item("common_camera03", 105, 115, "alert", "dog"),
    item("common_camera02", 300, 310),
    item("common_camera04", 100, 110),
  ]);
  assert.equal(grouped.length, 4);
  assert.ok(grouped.every((group) => group.items.length === 1));
});

test("an open segment overlaps another camera until now but does not absorb distant incidents", () => {
  const grouped = groupReviewItems([
    item("common_camera01", 100, null), item("common_camera02", 130, 145),
    item("common_camera03", 400, null),
  ], 450);
  assert.equal(grouped.length, 2);
  assert.equal(grouped[1].items.length, 2);
});

test("motion slots retain missing 30-second intervals on the one-hour timeline", () => {
  const slots = buildMotionSlots([
    { start_time: 0, motion: 20 }, { start_time: 60, motion: 10 },
  ], 3600);
  assert.equal(slots.length, 120);
  assert.deepEqual(slots.slice(0, 3).map((point) => point.start_time), [0, 30, 60]);
  assert.equal(slots[1].missing, true);
  assert.equal(slots[2].motion, 10);
});

test("successive segments from the same camera remain separate source incidents", () => {
  const grouped = groupReviewItems([
    item("common_camera01", 100, 110), item("common_camera01", 113, 125),
  ]);
  assert.equal(grouped.length, 2);
});
