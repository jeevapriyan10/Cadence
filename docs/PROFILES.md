# Cadence Network Profiles Reference

Cadence models railway operations through **typed network profiles** derived from [`NetworkProfile`](file:///e:/big/cadence/backend/cadence/profiles/base.py). Rather than hardcoding operational rules into the solver, Cadence delegates network topology, safety-adjacency definitions, minimum headways, task priority weights, and disruption penalties to profile instances.

This document serves as the in-depth specification for the three built-in profiles: **Metro**, **Local**, and **Mainline**, along with instructions for implementing custom profiles.

---

## Profile Summary Matrix

| Attribute | [MetroProfile](file:///e:/big/cadence/backend/cadence/profiles/metro.py) | [LocalProfile](file:///e:/big/cadence/backend/cadence/profiles/local.py) | [MainlineProfile](file:///e:/big/cadence/backend/cadence/profiles/mainline.py) |
|---|---|---|---|
| **System Analogy** | High-density urban rapid transit / metro loop lines (e.g., London Circle line, Tokyo Yamanote line) | Suburban commuter & regional corridors with mixed express/stopping services (e.g., S-Bahn, RER) | Mixed-traffic intercity trunk corridors with freight sidings and passing loops |
| **Default Topology** | Closed bidirectional loop (`loop`) | Linear trunk corridor (`linear`) | Linear corridor with passing loops (`linear_with_passing_loops`) |
| **Typical Section Count** | ~12 sections | ~15 sections | ~25 sections |
| **Minimum Headway** | 3 minutes | 8 min (base) / 10 min (mixed express-local) | 15 min (base) / 20 min (freight) |
| **Safety-Adjacency Invariant** | Graph connectivity preserved (loop remains unbroken in both directions) | No section serving both express and local trains becomes isolated (degree > 0) | Alternate bypass path remains operational between junction points (passing loop unstranded) |
| **Task Priority Weighting** | Emergency: `1000 + 50*p`<br>Routine: `1.0 + 0.1*p` | Emergency: `1000 + 50*p`<br>Express-impact: `25*p`<br>Local-only: `10*p` | Emergency: `2000 + 50*p`<br>Passenger: `30*p`<br>Freight: `10*p` |
| **Train Disruption Penalty** | Flat high penalty: `100.0 * priority` | Density-scaled penalty: `25.0 * priority * max(1, slot_density)` | Traffic-sensitive penalty: `75.0 * priority` (passenger), `10.0 * priority` (freight) |

---

## 1. Metro Profile (`MetroProfile`)

Source: [`backend/cadence/profiles/metro.py`](file:///e:/big/cadence/backend/cadence/profiles/metro.py)

### 1.1 Operating Model & Analogy
The Metro profile represents dense, high-frequency urban metro transit systems operating on closed or circular corridors. Urban metros feature high passenger volumes, short dwell times, and frequent trains running at tight headway intervals. 

Because metro systems often lack intermediate passing sidings, taking sections out of service can break network loop connectivity, stranding trains and cutting off circulation.

### 1.2 Safety-Adjacency Rule
In [`MetroProfile.is_safe_adjacency`](file:///e:/big/cadence/backend/cadence/profiles/metro.py#L16-L47), two sections cannot be blocked simultaneously if removing them disconnects the operational track network:

1. **Total Collapse**: If removing blocked sections leaves 0 operational sections, it is rejected.
2. **Isolation Collapse**: If the network originally had more than 2 sections, leaving only a single operational section is rejected.
3. **Loop Disconnection**: Removing the blocked sections must leave a subgraph where `simulated.is_connected()` is `True`. If the subgraph is partitioned into multiple disconnected components, loop connectivity is broken.

#### Worked Example
Consider a 4-section loop network: `S1 — S2 — S3 — S4 — S1`.
- **Case A: Adjacent Blockage (`S1` and `S2`)**
  - Blocked: `{S1, S2}`.
  - Remaining operational sections: `{S3, S4}`.
  - The link between `S3` and `S4` remains intact. The remaining subgraph is connected.
  - **Result**: `Safe` (`True`).
- **Case B: Opposite Blockage (`S1` and `S3`)**
  - Blocked: `{S1, S3}`.
  - Remaining operational sections: `{S2, S4}`.
  - `S2` was only connected to `S1` and `S3`. `S4` was only connected to `S1` and `S3`.
  - In the subgraph `{S2, S4}`, there is no edge connecting `S2` and `S4`. The graph is partitioned into two disjoint components.
  - **Result**: `Unsafe` (`False`: *"simultaneous blockage of sections ['S1', 'S3'] breaks loop connectivity in both directions"*).

### 1.3 Parameters
- **Headway Scale**: 3 minutes. Reflects moving-block or high-capacity fixed-block signaling with short train lengths and uniform braking profiles.
- **Priority Weights**:
  - Emergency maintenance: `1000.0 + priority * 50.0`. Emergency tasks immediately dominate solver objective placement.
  - Routine maintenance: `1.0 + priority * 0.1`. Near-flat weighting across routine tasks.
- **Disruption Penalty**: `100.0 * priority`. High fixed cost for delaying passenger metro trains.

---

## 2. Local Profile (`LocalProfile`)

Source: [`backend/cadence/profiles/local.py`](file:///e:/big/cadence/backend/cadence/profiles/local.py)

### 2.1 Operating Model & Analogy
The Local profile models suburban commuter rail and regional corridors balancing stopping local trains and skip-stop express passenger services. In these networks, central interchange stations frequently serve both express and local transfers.

### 2.2 Safety-Adjacency Rule
In [`LocalProfile.is_safe_adjacency`](file:///e:/big/cadence/backend/cadence/profiles/local.py#L16-L78), safety is governed by safeguarding mixed express/local interchange points:

1. Identify all sections that serve *both* express and local trains (either via node metadata `has_express_and_local` or dynamic inspection of passing `train_slots`).
2. Simulate the removal of the candidate blocked sections.
3. Verify that no mixed express/local section becomes completely isolated (`degree == 0` in the operational subgraph).

#### Worked Example
Consider a linear corridor: `S1 — S2 — S3 — S4`.
- Section `S2` serves both local commuter train `L1` and express train `E1`.
- If maintenance is scheduled simultaneously on `S1` and `S3`:
  - `S2` loses its edges to `S1` and `S3`.
  - `S2` has degree 0 in the operational subgraph.
  - **Result**: `Unsafe` (`False`: *"blocking sections ['S1', 'S3'] isolates section 'S2' which serves both express and local services"*).
- If maintenance is scheduled on `S1` and `S4`:
  - Operational subgraph retains `S2 — S3`. `S2` retains degree 1.
  - **Result**: `Safe` (`True`).

### 2.3 Parameters
- **Headway Scale**: 8 minutes base headway; expands to 10 minutes when an express train meets or follows a local train (`is_a_express != is_b_express`) to absorb differential speed braking profiles.
- **Priority Weights**:
  - Emergency: `1000.0 + priority * 50.0`.
  - Tasks affecting express routes: `25.0 * max(1, priority)`.
  - Tasks affecting local-only routes: `10.0 * max(1, priority)`.
- **Disruption Penalty**: Base `25.0 * priority` multiplied by the slot density (`slots_on_section`), scaling penalties up where traffic is densest.

---

## 3. Mainline Profile (`MainlineProfile`)

Source: [`backend/cadence/profiles/mainline.py`](file:///e:/big/cadence/backend/cadence/profiles/mainline.py)

### 2.1 Operating Model & Analogy
The Mainline profile represents long-distance, mixed-traffic trunk lines with periodic passing loops, freight sidings, and junction points. Long freight trains share trackage with high-speed passenger services. Passing loops provide dynamic passing opportunities where slower freight trains dwell while faster passenger trains bypass them.

### 2.2 Safety-Adjacency Rule
In [`MainlineProfile.is_safe_adjacency`](file:///e:/big/cadence/backend/cadence/profiles/mainline.py#L16-L63), the safety rule prevents **stranding passing loops**:

1. Extract all external neighbors adjacent to the blocked section candidate set.
2. For each pair of external neighbors that were connected before the blockage, compute whether an alternate bypass path still exists using shortest-path search in the subgraph.
3. If removing the candidate blocked sections eliminates the sole alternate route between any two junction points, the candidate blockage is rejected.

#### Worked Example
Consider a mainline trunk with a passing loop:
- Mainline path: `Junction_A — S_main — Junction_B`
- Loop bypass path: `Junction_A — S_loop — Junction_B`
- If maintenance is scheduled on `S_main`:
  - Trains can still route `Junction_A <-> Junction_B` via `S_loop`.
  - **Result**: `Safe` (`True`: *"passing loop intact; alternate bypass route exists"*).
- If maintenance is scheduled simultaneously on `S_main` and `S_loop`:
  - Both parallel paths are severed. No route connects `Junction_A` and `Junction_B`.
  - **Result**: `Unsafe` (`False`: *"strands passing loop; no alternate path exists between 'Junction_A' and 'Junction_B'"*).

### 2.3 Parameters
- **Headway Scale**: 15 minutes base; expands to 20 minutes if freight trains are involved (`is_freight=True`), accounting for long stopping distances and low acceleration.
- **Priority Weights**:
  - Emergency: `2000.0 + priority * 50.0`.
  - Passenger-tagged tasks: `30.0 * max(1, priority)`.
  - Freight tasks: `10.0 * max(1, priority)`.
- **Disruption Penalty**:
  - Passenger slots: `75.0 * priority`.
  - Freight slots: `10.0 * priority`.

---

## 4. Adding a New Profile

To implement a new railway operating profile, follow these steps:

### Step 1: Subclass `NetworkProfile`
Create a new file in [`backend/cadence/profiles/`](file:///e:/big/cadence/backend/cadence/profiles/) (e.g., `high_speed.py`):

```python
from typing import Any, Optional
from cadence.domain.graph import NetworkGraph
from cadence.domain.schemas import MaintenanceTaskSchema, TrainSlotSchema
from cadence.profiles.base import NetworkProfile

class HighSpeedProfile(NetworkProfile):
    profile_name: str = "high_speed"
    default_topology: str = "linear"

    def is_safe_adjacency(
        self,
        graph: NetworkGraph,
        section_a_id: str,
        section_b_id: str,
        currently_blocked: Optional[set[str]] = None,
        **kwargs: Any,
    ) -> tuple[bool, str]:
        # Custom topology and safety invariants
        blocked = {s for s in (section_a_id, section_b_id) if s}
        if currently_blocked:
            blocked |= set(currently_blocked)
        
        # In high-speed, maintain at least 2 clear sections between maintenance blocks
        # ... logic ...
        return True, "Safe: headway buffers maintained."

    def min_headway_minutes(
        self,
        train_slot_a: TrainSlotSchema,
        train_slot_b: TrainSlotSchema,
    ) -> int:
        return 5  # High-speed cab signaling minimum

    def priority_weight(self, task: MaintenanceTaskSchema) -> float:
        if task.is_emergency:
            return 3000.0 + float(task.priority) * 100.0
        return 15.0 * float(max(1, task.priority))

    def disruption_penalty(self, train_slot: TrainSlotSchema) -> float:
        return 200.0 * float(max(1, train_slot.priority))

    def default_topology_generator_hint(self) -> dict[str, Any]:
        return {
            "topology_type": "linear",
            "avg_section_count": 20,
            "bidirectional": True,
        }
```

### Step 2: Register in `ProfileRegistry`
Register the class in [`backend/cadence/profiles/registry.py`](file:///e:/big/cadence/backend/cadence/profiles/registry.py):

```python
from cadence.profiles.high_speed import HighSpeedProfile

ProfileRegistry.register(HighSpeedProfile)
```

Once registered, the profile is immediately selectable across the API (`POST /networks/generate`), CLI, synthetic generator, solver, and UI dashboard.
