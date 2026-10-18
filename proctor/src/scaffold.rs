//! Revealing a unit: its instructions, its notes, and a light workspace.
//! Never overwrites anything the student wrote.

use crate::course::CourseDir;
use crate::engine::protos_for;
use crate::journal::{self, Event};
use crate::lang;
use crate::render;
use crate::sig::Proto;
use crate::unit::{Rule, Scaffold, Unit};
use std::fs;
use std::path::Path;

pub struct Revealed {
    pub created: Vec<String>,
    /// Files that already existed and were left alone, with what to add.
    pub kept: Vec<(String, String)>,
}

fn write_new(
    root: &Path,
    rel: &str,
    body: &str,
    r: &mut Revealed,
    shown: &str,
) -> Result<(), String> {
    let p = root.join(rel);
    if p.exists() {
        return Ok(());
    }
    if let Some(d) = p.parent() {
        fs::create_dir_all(d).map_err(|e| format!("{}: {e}", d.display()))?;
    }
    fs::write(&p, body).map_err(|e| format!("{}: {e}", p.display()))?;
    r.created.push(format!("{shown}{}", rel.replace('\\', "/")));
    Ok(())
}

/// Writes the rendered instructions (and overview) even if they exist: the
/// student never edits them, and the record is the source of truth.
fn write_doc(root: &Path, rel: &Path, body: &str, r: &mut Revealed) -> Result<(), String> {
    let p = root.join(rel);
    if let Some(d) = p.parent() {
        fs::create_dir_all(d).map_err(|e| format!("{}: {e}", d.display()))?;
    }
    let existed = p.exists();
    fs::write(&p, body).map_err(|e| format!("{}: {e}", p.display()))?;
    if !existed {
        r.created.push(rel.to_string_lossy().replace('\\', "/"));
    }
    Ok(())
}

fn copy_scaffold(
    from: &Path,
    to: &Path,
    prefix: &str,
    r: &mut Revealed,
    shown: &str,
) -> Result<(), String> {
    let Ok(rd) = fs::read_dir(from) else {
        return Ok(());
    };
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        let rel = if prefix.is_empty() {
            name.clone()
        } else {
            format!("{prefix}/{name}")
        };
        if e.path().is_dir() {
            copy_scaffold(&e.path(), to, &rel, r, shown)?;
        } else {
            let body = fs::read(e.path()).map_err(|err| err.to_string())?;
            let dst = to.join(&rel);
            if !dst.exists() {
                if let Some(d) = dst.parent() {
                    fs::create_dir_all(d).map_err(|err| err.to_string())?;
                }
                fs::write(&dst, body).map_err(|err| err.to_string())?;
                r.created.push(format!("{shown}{rel}"));
            }
        }
    }
    Ok(())
}

pub fn reveal(c: &CourseDir, u: &Unit) -> Result<Revealed, String> {
    let mut r = Revealed {
        created: Vec::new(),
        kept: Vec::new(),
    };
    let root = &c.root;
    if let Some(pid) = &u.project {
        write_doc(
            root,
            &c.overview_rel(pid),
            &render::overview_md(c, pid),
            &mut r,
        )?;
    }
    write_doc(root, &c.subject_rel(u), &render::unit_md(c, u), &mut r)?;
    write_doc(
        root,
        Path::new(crate::rules::FILE),
        &crate::rules::rules_md(c),
        &mut r,
    )?;
    for n in &u.notes {
        let src = c.hidden().join("notes").join(format!("{n}.md"));
        let body = fs::read_to_string(&src).map_err(|e| format!("note `{n}`: {e}"))?;
        write_new(root, &format!("notes/{n}.md"), &body, &mut r, "")?;
    }
    if u.scaffold == Scaffold::None {
        return Ok(r);
    }
    let ws_name = c.workspace_name(u);
    let ws = c.workspace(u);
    if !ws.exists() {
        fs::create_dir_all(&ws).map_err(|e| format!("{}: {e}", ws.display()))?;
        r.created.push(format!("{ws_name}/"));
    }
    copy_scaffold(
        &c.hidden().join("scaffold").join(&u.id),
        &ws,
        "",
        &mut r,
        &format!("{ws_name}/"),
    )?;
    let lang = c.language(u);
    let own: Vec<Proto> = protos_for(c, &[u])?;
    if let (Some(stubs), false) = (&u.stubs, own.is_empty()) {
        let path = ws.join(stubs);
        if path.exists() {
            let body = lang::stub_source(&lang, &own).unwrap_or_default();
            r.kept.push((format!("{ws_name}/{stubs}"), body));
        } else if let Some(body) = lang::stub_source(&lang, &own) {
            let body = match lang.as_str() {
                "c" => {
                    let header = u
                        .files
                        .iter()
                        .find(|f| {
                            f.rule == Rule::Required
                                && f.path.ends_with(".h")
                                && !f.path.contains('*')
                        })
                        .map(|f| f.path.rsplit('/').next().unwrap_or(&f.path).to_string());
                    match header {
                        Some(h) => format!("#include \"{h}\"\n\n{body}"),
                        None => body,
                    }
                }
                _ => body,
            };
            write_new(&ws, stubs, &body, &mut r, &format!("{ws_name}/"))?;
        }
    }
    if u.scaffold == Scaffold::Full {
        let chain_protos = protos_for(c, &c.chain(u))?;
        for f in u
            .files
            .iter()
            .filter(|f| f.rule == Rule::Required && !f.path.contains('*'))
        {
            let body = if lang == "c" && f.path.ends_with(".h") {
                lang::c_header(&f.path, &chain_protos)
            } else {
                String::new()
            };
            write_new(&ws, &f.path, &body, &mut r, &format!("{ws_name}/"))?;
        }
    }
    Ok(r)
}

/// Unlocks `u`: reveals it and records the event.
pub fn unlock(c: &CourseDir, u: &Unit) -> Result<Revealed, String> {
    let r = reveal(c, u)?;
    journal::append(
        &c.root,
        &Event {
            t: journal::now(),
            ev: "unlock".into(),
            unit: u.id.clone(),
            ..Default::default()
        },
    )?;
    Ok(r)
}
