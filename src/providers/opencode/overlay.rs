//! Hand-written seam over the generated OpenCode Go catalog overlay.
//!
//! When a sync has run for this working copy, [`overlay_generated`] carries the
//! live catalog (`SYNCED = true`) and is used *exclusively* for resolution and
//! advertisement — a strict mirror of the live list. When no sync has run the
//! overlay is empty (`SYNCED = false`) and the upstream static catalog in
//! [`super::model`] remains the fallback.
//!
//! This module is never regenerated; [`overlay_generated`] is.

use super::model::{EndpointKind, MODEL_PREFIX, ModelSpec};

/// How a synced entry is advertised on the `models` listing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Advertisement {
    /// Advertised under both the bare id and the `opencode-go/` qualified form.
    BareAndQualified,
    /// Advertised only under the `opencode-go/` qualified form because the bare
    /// id is owned by another provider in this build.
    QualifiedOnly,
}

/// One routable model in the synced catalog.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SyncedEntry {
    pub id: &'static str,
    pub endpoint: EndpointKind,
    pub advert: Advertisement,
}

/// Whether this build carries a synced catalog overlay.
pub fn synced() -> bool {
    super::overlay_generated::SYNCED
}

/// The synced entries (empty when [`synced`] is false).
pub fn active_entries() -> &'static [SyncedEntry] {
    super::overlay_generated::ENTRIES
}

/// Resolve a raw incoming model id against `entries`.
///
/// Strips [`MODEL_PREFIX`] and matches by id, mirroring today's static
/// `resolve`. Strict: only ids present in `entries` resolve.
pub fn resolve_in(entries: &[SyncedEntry], raw: &str) -> Option<ModelSpec> {
    let id = raw.strip_prefix(MODEL_PREFIX).unwrap_or(raw);
    entries
        .iter()
        .find(|entry| entry.id == id)
        .map(|entry| ModelSpec {
            id: entry.id,
            endpoint: entry.endpoint,
        })
}

/// The advertised model list for `entries`.
///
/// Each [`Advertisement::BareAndQualified`] entry contributes its bare id; every
/// entry contributes its `opencode-go/` qualified form. Sorted ascending.
pub fn advertised_from(entries: &[SyncedEntry]) -> Vec<String> {
    let mut result = Vec::with_capacity(entries.len() * 2);
    for entry in entries {
        if matches!(entry.advert, Advertisement::BareAndQualified) {
            result.push(entry.id.to_string());
        }
        result.push(format!("{MODEL_PREFIX}{}", entry.id));
    }
    result.sort_unstable();
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &[SyncedEntry] = &[
        SyncedEntry {
            id: "alpha-model",
            endpoint: EndpointKind::ChatCompletions,
            advert: Advertisement::BareAndQualified,
        },
        SyncedEntry {
            id: "beta-model",
            endpoint: EndpointKind::Messages,
            advert: Advertisement::QualifiedOnly,
        },
        SyncedEntry {
            id: "gamma-model",
            endpoint: EndpointKind::Responses,
            advert: Advertisement::BareAndQualified,
        },
    ];

    #[test]
    fn resolve_in_resolves_both_advertisement_forms() {
        for raw in ["alpha-model", "opencode-go/alpha-model"] {
            let spec = resolve_in(FIXTURE, raw).expect("bare-and-qualified entry resolves");
            assert_eq!(spec.id, "alpha-model");
            assert_eq!(spec.endpoint, EndpointKind::ChatCompletions);
        }
        for raw in ["beta-model", "opencode-go/beta-model"] {
            let spec = resolve_in(FIXTURE, raw).expect("qualified-only entry resolves");
            assert_eq!(spec.id, "beta-model");
            assert_eq!(spec.endpoint, EndpointKind::Messages);
        }
    }

    #[test]
    fn resolve_in_is_exclusive_to_the_overlay() {
        // Ids outside the overlay must not resolve when the overlay is the source,
        // even if they exist in the static upstream catalog.
        assert!(resolve_in(FIXTURE, "glm-5.2").is_none());
        assert!(resolve_in(FIXTURE, "opencode-go/minimax-m3").is_none());
        assert!(resolve_in(FIXTURE, "not-a-model").is_none());
    }

    #[test]
    fn advertised_from_covers_both_advertisement_forms() {
        let advertised = advertised_from(FIXTURE);
        // BareAndQualified entries advertise both forms.
        assert!(advertised.contains(&"alpha-model".to_string()));
        assert!(advertised.contains(&"opencode-go/alpha-model".to_string()));
        // QualifiedOnly entries advertise only the qualified form.
        assert!(!advertised.contains(&"beta-model".to_string()));
        assert!(advertised.contains(&"opencode-go/beta-model".to_string()));
        assert!(advertised.contains(&"gamma-model".to_string()));
        assert!(advertised.contains(&"opencode-go/gamma-model".to_string()));
    }

    #[test]
    fn advertised_from_is_sorted_and_unique() {
        let advertised = advertised_from(FIXTURE);
        let mut sorted = advertised.clone();
        sorted.sort_unstable();
        assert_eq!(advertised, sorted);
        let mut deduped = advertised.clone();
        deduped.dedup();
        assert_eq!(deduped.len(), advertised.len());
    }

    #[test]
    fn advertised_from_qualified_only_form_is_symmetric_with_resolution() {
        // Every advertised id must resolve through resolve_in.
        for model in advertised_from(FIXTURE) {
            assert!(
                resolve_in(FIXTURE, &model).is_some(),
                "advertised {model} must resolve"
            );
        }
    }
}
