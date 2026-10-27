import assert from "node:assert/strict";
import { test } from "node:test";
import { describeFactSource, factIsNew, groupFacts } from "./memoryFacts.ts";

test("sources read as plain phrases", () => {
  assert.equal(describeFactSource("run 12, you, in chat"), "You told Lumen");
  assert.equal(describeFactSource("you, answering a question"), "Your answer during a task");
  assert.equal(describeFactSource("from Resume.pdf"), "From Resume.pdf");
  assert.equal(describeFactSource("added by you"), "Added by you");
  assert.equal(describeFactSource(""), "");
  assert.equal(describeFactSource(null), "");
});

test("only facts Lumen learned recently are new", () => {
  const now = new Date("2026-10-03T15:00:00Z");
  assert.equal(factIsNew({ learned_on: "2026-10-03", source: "you, in chat" }, now), true);
  assert.equal(factIsNew({ learned_on: "2026-09-20", source: "you, in chat" }, now), false);
  assert.equal(factIsNew({ learned_on: "2026-10-03", source: "added by you" }, now), false);
  assert.equal(factIsNew({ learned_on: null, source: "you, in chat" }, now), false);
});

test("facts group by section in order and empty sections drop", () => {
  const groups = groupFacts({
    sections: [
      { key: "about", title: "About you" },
      { key: "contact", title: "Contact" },
      { key: "work", title: "Work" }
    ],
    facts: [
      { key: "years_python", label: "Years python", value: "7", section: "work", learned_on: null, source: null },
      { key: "name", label: "Name", value: "Jordan", section: "about", learned_on: null, source: null }
    ]
  });
  assert.deepEqual(
    groups.map((group) => [group.key, group.facts.map((fact) => fact.key)]),
    [
      ["about", ["name"]],
      ["work", ["years_python"]]
    ]
  );
});
