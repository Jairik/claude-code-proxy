//! Serve-time catalog-sync pre-flight (the hook contract).
//!
//! The `serve` path runs the OpenCode Go catalog sync before it builds the
//! provider registry. The sync is a separate process (the kit's `sync.py`); its
//! exit code tells the serve path what to do:
//!
//! * `0` — no change: keep serving the current binary.
//! * `2` — updated *and* installed: re-exec the freshly installed binary with
//!   identical arguments (loop-guarded via [`JUST_EXECED_ENV`]).
//! * `1` / any other code / spawn error — warn once and serve the existing
//!   binary.
//!
//! The decision logic ([`plan`], [`classify`]) is a pure function of the
//! environment and the script's result, kept separate from process spawning
//! ([`run_preflight`]) so every branch is unit-testable.
//!
//! Safety valves: [`ON_SERVE_ENV`]`=0` disables the pre-flight entirely;
//! [`JUST_EXECED_ENV`] makes the re-executed process skip the check exactly
//! once. Locations are environment-overridable ([`SCRIPT_ENV`], [`KIT_ENV`],
//! [`REPO_ENV`]).

use std::collections::HashMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Kill switch: set to `0` to disable the serve pre-flight entirely.
pub const ON_SERVE_ENV: &str = "CCP_SYNC_ON_SERVE";
/// Loop guard: set by the re-exec so the child skips the check exactly once.
pub const JUST_EXECED_ENV: &str = "CCP_SYNC_JUST_EXECED";
/// Override for the sync script path (default `<kit>/sync.py`).
pub const SCRIPT_ENV: &str = "CCP_SYNC_SCRIPT";
/// Override for the kit root (default `~/.local/share/claude-code-proxy-sync`).
pub const KIT_ENV: &str = "CCP_SYNC_KIT";
/// Override for the working-copy root (default `~/repos/claude-code-proxy`).
pub const REPO_ENV: &str = "CCP_SYNC_REPO";

/// Kit location relative to `$HOME` when [`KIT_ENV`] is unset.
const DEFAULT_KIT: &str = ".local/share/claude-code-proxy-sync";
/// Working-copy location relative to `$HOME` when [`REPO_ENV`] is unset.
const DEFAULT_REPO: &str = "repos/claude-code-proxy";

/// Why the pre-flight was skipped without running the script.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SkipReason {
    /// `CCP_SYNC_ON_SERVE=0`.
    KillSwitch,
    /// `CCP_SYNC_JUST_EXECED` set — this process was just re-executed.
    JustExecuted,
    /// The working copy does not exist.
    MissingRepo(PathBuf),
}

impl SkipReason {
    /// Human-readable description used in the startup summary.
    pub fn describe(&self) -> String {
        match self {
            SkipReason::KillSwitch => format!("disabled by {ON_SERVE_ENV}=0"),
            SkipReason::JustExecuted => {
                format!("already re-executed ({JUST_EXECED_ENV} set)")
            }
            SkipReason::MissingRepo(path) => {
                format!("working copy not found: {}", path.display())
            }
        }
    }
}

/// Resolved paths for the pre-flight.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookPaths {
    pub script: PathBuf,
    pub repo: PathBuf,
}

/// The pure pre-flight plan: either run the script, or skip for a reason.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Preflight {
    Run { script: PathBuf, repo: PathBuf },
    Skip(SkipReason),
}

/// The result of spawning the sync script.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScriptResult {
    /// The script exited with this code.
    Exited(i32),
    /// The script could not be spawned.
    SpawnError(String),
}

/// The serve action chosen from the pre-flight result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// Sync found no change; serve the current binary.
    NoChange,
    /// Sync updated and installed a new binary; re-exec it.
    Updated,
    /// Sync failed; warn once and serve the current binary.
    Failed(String),
    /// The pre-flight was skipped.
    Skipped(SkipReason),
}

impl Outcome {
    /// A single-line startup summary for this outcome.
    pub fn summary(&self) -> String {
        match self {
            Outcome::NoChange => "ccp-sync: catalog up to date (no change)".to_string(),
            Outcome::Updated => "ccp-sync: catalog updated and installed; re-executing".to_string(),
            Outcome::Failed(reason) => {
                format!("ccp-sync: {reason}; serving the existing binary")
            }
            Outcome::Skipped(reason) => format!("ccp-sync: skipped ({})", reason.describe()),
        }
    }

    /// Whether this outcome should be logged at `warn` level.
    pub fn is_failure(&self) -> bool {
        matches!(self, Outcome::Failed(_))
    }
}

/// Resolve the script and repo paths from the environment (pure).
pub fn resolve_paths(env: &HashMap<String, String>) -> HookPaths {
    let home = env
        .get("HOME")
        .or_else(|| env.get("USERPROFILE"))
        .cloned()
        .unwrap_or_else(|| "/".to_string());
    let kit = env
        .get(KIT_ENV)
        .cloned()
        .unwrap_or_else(|| join_home(&home, DEFAULT_KIT));
    let script = env
        .get(SCRIPT_ENV)
        .cloned()
        .unwrap_or_else(|| format!("{kit}/sync.py"));
    let repo = env
        .get(REPO_ENV)
        .cloned()
        .unwrap_or_else(|| join_home(&home, DEFAULT_REPO));
    HookPaths {
        script: PathBuf::from(script),
        repo: PathBuf::from(repo),
    }
}

fn join_home(home: &str, relative: &str) -> String {
    let trimmed = home.trim_end_matches(['/', '\\']);
    format!("{trimmed}/{relative}")
}

/// Decide the pre-flight plan from the environment.
///
/// Pure: repository existence is passed in as `repo_exists` so the missing-repo
/// branch is unit-testable without touching the filesystem. The kill switch and
/// loop guard short-circuit before any path is resolved.
pub fn plan(env: &HashMap<String, String>, repo_exists: bool) -> Preflight {
    if env.get(ON_SERVE_ENV).map(String::as_str) == Some("0") {
        return Preflight::Skip(SkipReason::KillSwitch);
    }
    // Presence-based guard: CCP_SYNC_JUST_EXECED set to any value skips; only the hook's own re-exec sets it.
    if env.contains_key(JUST_EXECED_ENV) {
        return Preflight::Skip(SkipReason::JustExecuted);
    }
    let paths = resolve_paths(env);
    if !repo_exists {
        return Preflight::Skip(SkipReason::MissingRepo(paths.repo));
    }
    Preflight::Run {
        script: paths.script,
        repo: paths.repo,
    }
}

/// Classify the sync script's result into a serve action (pure).
pub fn classify(result: ScriptResult) -> Outcome {
    match result {
        ScriptResult::Exited(0) => Outcome::NoChange,
        ScriptResult::Exited(2) => Outcome::Updated,
        ScriptResult::Exited(code) => {
            Outcome::Failed(format!("sync script exited with code {code}"))
        }
        ScriptResult::SpawnError(error) => {
            Outcome::Failed(format!("could not run sync script: {error}"))
        }
    }
}

/// Run the serve pre-flight: resolve the plan from the process environment,
/// spawn the sync script when the plan says so, and classify the result.
pub fn run_preflight() -> Outcome {
    let env: HashMap<String, String> = std::env::vars().collect();
    run_preflight_with_env(&env, |path| path.is_dir())
}

/// [`run_preflight`] with an injected environment and repository-existence
/// probe. Kept public so tests can drive the spawn seam deterministically.
pub fn run_preflight_with_env(
    env: &HashMap<String, String>,
    repo_exists: impl Fn(&Path) -> bool,
) -> Outcome {
    let paths = resolve_paths(env);
    match plan(env, repo_exists(&paths.repo)) {
        Preflight::Skip(reason) => Outcome::Skipped(reason),
        Preflight::Run { script, .. } => classify(spawn_script(&script)),
    }
}

fn spawn_script(script: &Path) -> ScriptResult {
    // The kit's sync script is a Python 3 program (stdlib only); run it with
    // the system interpreter. Its stdio is inherited so progress is visible.
    match Command::new("python3").arg(script).status() {
        Ok(status) => ScriptResult::Exited(status.code().unwrap_or(-1)),
        Err(error) => ScriptResult::SpawnError(error.to_string()),
    }
}

/// Strip the Linux `" (deleted)"` suffix that the kernel appends to the
/// `/proc/self/exe` link (and therefore `current_exe()`) once the running
/// binary's inode has been unlinked. The sync's atomic install replaces the
/// binary via `os.replace`, which unlinks the old inode; stripping the suffix
/// recovers the original path, which now names the freshly installed file.
fn strip_deleted_suffix(path: &Path) -> PathBuf {
    match path
        .to_str()
        .and_then(|text| text.strip_suffix(" (deleted)"))
    {
        Some(stripped) => PathBuf::from(stripped),
        None => path.to_path_buf(),
    }
}

/// Re-execute the current binary with identical arguments.
///
/// The process image is replaced on success, so this never returns; on failure
/// it returns an error string so the caller can warn and keep serving. The
/// child gets [`JUST_EXECED_ENV`] set, which makes it skip the pre-flight
/// exactly once (loop guard).
///
/// The re-exec target is `current_exe()` with any trailing `" (deleted)"`
/// suffix removed: after the sync's atomic install the running process's old
/// inode is gone, so `current_exe()` returns `"<path> (deleted)"`, while the
/// stripped path names the newly installed binary at the same location. If that
/// resolved path does not exist, this fails gracefully (the caller warns and
/// keeps serving the existing binary).
pub fn reexec_self() -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|error| error.to_string())?;
    let target = strip_deleted_suffix(&exe);
    if !target.exists() {
        return Err(format!(
            "re-exec target {} does not exist",
            target.display()
        ));
    }
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // `exec` only returns when it fails to replace the process image.
        let error = Command::new(&target)
            .args(&args)
            .env(JUST_EXECED_ENV, "1")
            .exec();
        Err(format!("re-exec failed: {error}"))
    }

    #[cfg(not(unix))]
    {
        let _ = (&target, &args);
        Err("re-exec is not supported on this platform".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        let mut map: HashMap<String, String> = [("HOME", "/home/tester")]
            .into_iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        for (key, value) in pairs {
            map.insert((*key).to_string(), (*value).to_string());
        }
        map
    }

    #[test]
    fn kill_switch_disables_sync() {
        let plan = plan(&env(&[(ON_SERVE_ENV, "0")]), true);
        assert_eq!(plan, Preflight::Skip(SkipReason::KillSwitch));
    }

    #[test]
    fn kill_switch_takes_precedence_over_loop_guard_and_repo() {
        let plan = plan(&env(&[(ON_SERVE_ENV, "0"), (JUST_EXECED_ENV, "1")]), false);
        assert_eq!(plan, Preflight::Skip(SkipReason::KillSwitch));
    }

    #[test]
    fn loop_guard_skips_after_reexec() {
        let plan = plan(&env(&[(JUST_EXECED_ENV, "1")]), true);
        assert_eq!(plan, Preflight::Skip(SkipReason::JustExecuted));
    }

    #[test]
    fn missing_repo_skips_with_the_resolved_path() {
        let plan = plan(&env(&[(REPO_ENV, "/nonexistent")]), false);
        assert_eq!(
            plan,
            Preflight::Skip(SkipReason::MissingRepo(PathBuf::from("/nonexistent")))
        );
    }

    #[test]
    fn existing_repo_runs_with_home_relative_defaults() {
        let plan = plan(&env(&[]), true);
        assert_eq!(
            plan,
            Preflight::Run {
                script: PathBuf::from("/home/tester/.local/share/claude-code-proxy-sync/sync.py"),
                repo: PathBuf::from("/home/tester/repos/claude-code-proxy"),
            }
        );
    }

    #[test]
    fn explicit_env_overrides_paths() {
        let plan = plan(
            &env(&[
                (KIT_ENV, "/kit"),
                (SCRIPT_ENV, "/kit/custom.py"),
                (REPO_ENV, "/work/copy"),
            ]),
            true,
        );
        assert_eq!(
            plan,
            Preflight::Run {
                script: PathBuf::from("/kit/custom.py"),
                repo: PathBuf::from("/work/copy"),
            }
        );
    }

    #[test]
    fn classify_exit_zero_is_no_change() {
        assert_eq!(classify(ScriptResult::Exited(0)), Outcome::NoChange);
    }

    #[test]
    fn classify_exit_two_is_updated() {
        assert_eq!(classify(ScriptResult::Exited(2)), Outcome::Updated);
    }

    #[test]
    fn classify_exit_one_is_failed() {
        assert_eq!(
            classify(ScriptResult::Exited(1)),
            Outcome::Failed("sync script exited with code 1".to_string())
        );
    }

    #[test]
    fn classify_other_exit_is_failed() {
        assert_eq!(
            classify(ScriptResult::Exited(7)),
            Outcome::Failed("sync script exited with code 7".to_string())
        );
    }

    #[test]
    fn classify_spawn_error_is_failed() {
        assert_eq!(
            classify(ScriptResult::SpawnError("boom".to_string())),
            Outcome::Failed("could not run sync script: boom".to_string())
        );
    }

    #[test]
    fn summaries_are_single_line_and_identify_the_outcome() {
        let cases = [
            (Outcome::NoChange, "no change"),
            (Outcome::Updated, "re-executing"),
            (
                Outcome::Failed("boom".to_string()),
                "serving the existing binary",
            ),
            (
                Outcome::Skipped(SkipReason::KillSwitch),
                "CCP_SYNC_ON_SERVE=0",
            ),
            (
                Outcome::Skipped(SkipReason::MissingRepo(PathBuf::from("/gone"))),
                "working copy not found",
            ),
        ];
        for (outcome, needle) in cases {
            let summary = outcome.summary();
            assert!(
                summary.starts_with("ccp-sync:"),
                "summary {summary:?} must be tagged"
            );
            assert!(
                !summary.contains('\n'),
                "summary {summary:?} must be one line"
            );
            assert!(
                summary.contains(needle),
                "summary {summary:?} must mention {needle:?}"
            );
        }
    }

    #[test]
    fn failure_outcomes_are_flagged_for_warning() {
        assert!(Outcome::Failed("x".to_string()).is_failure());
        assert!(!Outcome::NoChange.is_failure());
        assert!(!Outcome::Updated.is_failure());
        assert!(!Outcome::Skipped(SkipReason::KillSwitch).is_failure());
    }

    #[test]
    fn run_preflight_with_env_skips_when_repo_is_missing() {
        let outcome = run_preflight_with_env(&env(&[(REPO_ENV, "/nonexistent")]), |_| false);
        assert_eq!(
            outcome,
            Outcome::Skipped(SkipReason::MissingRepo(PathBuf::from("/nonexistent")))
        );
    }

    #[test]
    fn strip_deleted_suffix_recovers_the_original_path() {
        assert_eq!(
            strip_deleted_suffix(Path::new("/x/y (deleted)")),
            PathBuf::from("/x/y")
        );
    }

    #[test]
    fn strip_deleted_suffix_leaves_a_normal_path_unchanged() {
        assert_eq!(
            strip_deleted_suffix(Path::new("/usr/local/bin/claude-code-proxy")),
            PathBuf::from("/usr/local/bin/claude-code-proxy")
        );
    }

    #[test]
    fn strip_deleted_suffix_only_matches_a_trailing_suffix() {
        // A `(deleted)` fragment that is not the trailing suffix is preserved.
        assert_eq!(
            strip_deleted_suffix(Path::new("/x/y (deleted)/bin")),
            PathBuf::from("/x/y (deleted)/bin")
        );
    }
}
