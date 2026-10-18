//! skillcheck: levelup's offline grader. Reads compact unit specs from a
//! course's hidden `.skillcheck/` directory and checks the student's work
//! with no network and no LLM.

pub mod ask;
pub mod course;
pub mod defaults;
pub mod docker;
pub mod engine;
pub mod expr;
pub mod gens;
pub mod harness;
pub mod journal;
pub mod lang;
pub mod program;
pub mod progress;
pub mod render;
pub mod report;
pub mod run;
pub mod scaffold;
pub mod scan;
pub mod selftest;
pub mod sig;
pub mod structure;
pub mod unit;
pub mod value;
