"""Code-owned metadata stays byte compatible without repeated schema generation."""
from dataclasses import FrozenInstanceError, replace

from pydantic import BaseModel, ConfigDict
import pytest

from business_capabilities.registration import create_business_registry
from core.config import Settings
from services.business_registry import (
    AdapterRegistration, CapabilityDefinition, ExecutionContract, ProviderProfile,
)


class Argument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    count: int


def small_registration(**changes):
    return AdapterRegistration(adapter_id='synthetic',
        profile=ProviderProfile(profile_revision='a' * 64, mode='BLIND'),
        capabilities=(CapabilityDefinition(name='synthetic.read', argument_model=Argument),),
        adapter=object(), **changes)


def test_complete_crm_catalog_matches_prechange_golden_bytes():
    # Captured on AI111 before replacing either property. These are Business
    # catalog revisions, not historical Managed request fingerprints (PR82).
    entry = create_business_registry(Settings(_env_file=None,
        business_capabilities_enabled=True, business_crm_url='http://synthetic.invalid'), None)['chotacrm']
    assert entry.legacy_catalog_revision == '7aec7a1afe419db8f4e65576cd01d49f73e6730f14005bfe7c19186518f651ce'
    assert entry.catalog_revision == '475dbfcb13170a141de1321f4c0cb338cb3be04b449d602a3b6c571b417d9bd4'


@pytest.mark.parametrize('mode', ['REFERENCE_V1', 'SEMANTIC_V1'])
def test_repeated_revision_and_acceptance_checks_do_not_regenerate_schema(monkeypatch, mode):
    original = Argument.model_json_schema.__func__
    generated = []

    def schema(cls, *args, **kwargs):
        generated.append(cls)
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(Argument, 'model_json_schema', classmethod(schema))
    entry = small_registration(confirmation_mode=mode)
    revision, legacy = entry.catalog_revision, entry.legacy_catalog_revision
    assert (legacy, revision) == {
        'REFERENCE_V1': (
            '200136d0e66a1865a9b22a482aece095f046e72609b43a3a60c43dd2f22e8c60',
            '55e00e1ca87a5856e0c219c3734ea1ad6bb48c379416a4afb31c665261cd2aae'),
        'SEMANTIC_V1': (
            'bb911b3824ed873801f3edd03f6a4f9b12ac57b439fafaee15d2121bd633b767',
            'ae84e1a838d2decf3b187825a835e3ccbe934a3aaa37382fcd606963c731fc29'),
    }[mode]
    assert generated == [Argument]
    for _ in range(20):
        assert entry.catalog_revision == revision and entry.legacy_catalog_revision == legacy
        assert entry.accepts_catalog(revision) and not entry.accepts_catalog('f' * 64)
    assert generated == [Argument]


@pytest.mark.parametrize('change', ['profile', 'confirmation_mode', 'schema', 'schema_version',
                                  'confirmation_required', 'public_fields', 'execution_contract'])
def test_replacement_recomputes_changed_catalog_and_rejects_old_revision(change):
    entry = small_registration()
    previous = entry.catalog_revision
    definition = entry.capabilities[0]

    class RevisedArgument(Argument):
        extra_count: int

    updates = {
        'profile': dict(profile=entry.profile.model_copy(update={'proposal_ttl_seconds': 301})),
        'confirmation_mode': dict(confirmation_mode='SEMANTIC_V1'),
        'schema': dict(capabilities=(replace(definition, argument_model=RevisedArgument),)),
        'schema_version': dict(capabilities=(replace(definition, schema_version=2),)),
        'confirmation_required': dict(capabilities=(replace(definition, requires_confirmation=True),)),
        'public_fields': dict(capabilities=(replace(definition, public_fields=('count',)),)),
        'execution_contract': dict(execution_contract=ExecutionContract(
            expected_facts='CONDITIONAL_EXPECTED_FACTS_V1')),
    }
    replacement = replace(entry, **updates[change])
    assert replacement.catalog_revision != previous
    assert not replacement.accepts_catalog(previous)
    assert entry.catalog_revision == previous
    assert replacement.accepts_catalog(replacement.catalog_revision)


def test_retained_exact_legacy_policy_is_unchanged_after_cache_is_populated():
    entry = small_registration()
    current, legacy = entry.catalog_revision, entry.legacy_catalog_revision
    compatible = replace(entry, retained_catalog_revisions=(legacy,))
    assert compatible.catalog_revision == current
    assert compatible.accepts_catalog(legacy) and not entry.accepts_catalog(legacy)
    with pytest.raises(ValueError, match='Exact conservative'):
        replace(entry, retained_catalog_revisions=('f' * 64,))


def test_cached_revisions_still_cannot_be_assigned_or_shared_across_instances():
    first, second = small_registration(), small_registration()
    expected = first.catalog_revision
    assert 'catalog_revision' not in second.__dict__
    with pytest.raises(FrozenInstanceError):
        first.catalog_revision = 'f' * 64
    assert second.catalog_revision == expected
    assert replace(first, adapter=object()).catalog_revision == expected
