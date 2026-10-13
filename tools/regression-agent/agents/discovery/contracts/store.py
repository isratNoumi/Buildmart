"""Read and write contracts.

    baseline/discovery/contracts/<page_id>.json        the current contract (draft or approved)
    baseline/discovery/contracts/<page_id>.draft.json  a newer draft waiting to replace an approved one
"""

from __future__ import annotations

from pathlib import Path

from .models import ContractStatus, PageContract


def contract_path(contracts_dir: Path, page_id: str) -> Path:
    return contracts_dir / f"{page_id}.json"


def draft_path(contracts_dir: Path, page_id: str) -> Path:
    return contracts_dir / f"{page_id}.draft.json"


def load_contract(contracts_dir: Path, page_id: str) -> PageContract | None:
    path = contract_path(contracts_dir, page_id)
    if not path.exists():
        return None
    return PageContract.model_validate_json(path.read_text(encoding="utf-8"))


def load_all(contracts_dir: Path) -> list[PageContract]:
    return [
        PageContract.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(contracts_dir.glob("*.json"))
        if not path.name.endswith(".draft.json")
    ]


def write_contract(contracts_dir: Path, contract: PageContract) -> Path:
    contracts_dir.mkdir(parents=True, exist_ok=True)
    path = contract_path(contracts_dir, contract.page_id)
    path.write_text(contract.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def save_draft(contracts_dir: Path, contract: PageContract) -> Path:
    """Save agent output. An approved contract is never overwritten: the new version waits beside it."""
    existing = load_contract(contracts_dir, contract.page_id)
    contract.status = ContractStatus.DRAFT
    if existing and existing.status is ContractStatus.APPROVED:
        contract.version = existing.version + 1
        path = draft_path(contracts_dir, contract.page_id)
        path.write_text(contract.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path
    if existing:
        contract.version = existing.version
    return write_contract(contracts_dir, contract)


def approve(contracts_dir: Path, page_id: str, allow_gaps: bool = False) -> PageContract:
    """Approve the pending draft (or the current draft contract). Raises if gaps remain."""
    pending = draft_path(contracts_dir, page_id)
    source = pending if pending.exists() else contract_path(contracts_dir, page_id)
    if not source.exists():
        raise FileNotFoundError(f"No contract for '{page_id}'")
    contract = PageContract.model_validate_json(source.read_text(encoding="utf-8"))
    if contract.status is ContractStatus.APPROVED:
        return contract
    gaps = contract.gaps()
    if gaps and not allow_gaps:
        raise ValueError(f"{len(gaps)} gap(s) remain, e.g. {gaps[0]}. Fill them or pass --allow-gaps.")
    contract.status = ContractStatus.APPROVED
    write_contract(contracts_dir, contract)
    pending.unlink(missing_ok=True)
    return contract
