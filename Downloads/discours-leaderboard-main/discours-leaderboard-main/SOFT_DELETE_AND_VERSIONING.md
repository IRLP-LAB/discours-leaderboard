# Soft Delete & Dataset Versioning Implementation

## Overview
This implementation adds soft delete functionality and dataset versioning to the system:
- **Soft Delete**: Data is marked as deleted but retained in the database
- **Dataset Versioning**: Multiple versions of datasets can coexist; only the latest version is used for evaluation
- **Cascade Delete**: Deleting a language also marks its associated datasets as deleted

## Features Implemented

### 1. **Soft Delete for Languages** ✅
**Database**:
- Added `is_deleted BOOLEAN DEFAULT FALSE` column to `languages` table
- Added index on `is_deleted` for fast filtering

**Behavior**:
- When admin clicks "Archive Language" button, confirmation dialog appears
- Language marked with `is_deleted = TRUE` instead of hard delete
- Associated gold datasets automatically marked as deleted
- Deleted languages don't appear in UI or dashboards
- Data permanently retained in database for audit/recovery

**Code Changes**:
```python
# Soft delete instead of hard delete
cursor.execute(
    "UPDATE languages SET is_deleted = TRUE WHERE id = %s",
    (language_id,)
)
# Cascade: Mark associated datasets as deleted
cursor.execute(
    "UPDATE gold_datasets SET is_deleted = TRUE WHERE language_id = %s",
    (language_id,)
)
```

### 2. **Soft Delete for Gold Datasets** ✅
**Database**:
- Added `is_deleted BOOLEAN DEFAULT FALSE` column to `gold_datasets` table
- Added index on `is_deleted` for fast filtering

**Behavior**:
- When admin clicks "Archive" button next to a dataset, confirmation dialog appears
- Dataset marked with `is_deleted = TRUE` instead of hard delete
- Evaluations using deleted datasets are not shown in leaderboards
- Data retained in database
- Previous versions of datasets can be archived while keeping new versions active

**Code Changes**:
```python
# Soft delete: Mark dataset as deleted
cursor.execute(
    "UPDATE gold_datasets SET is_deleted = TRUE WHERE id = %s",
    (dataset_id,)
)
```

### 3. **Dataset Versioning** ✅
**Database**:
- Added `version INT DEFAULT 1` column to `gold_datasets` table
- Added index on `version` for efficient queries

**File Naming**:
- Datasets now saved as: `v{version}_{timestamp}_{original_filename}`
- Example: `v1_20251111_120530_coreference_data.txt` → `v2_20251111_120545_coreference_data.txt`

**Behavior**:
- When uploading a new dataset for a language, version increments automatically
- Latest version retrieved from database based on MAX(version) query
- Only latest non-deleted version used for evaluations
- Evaluations validated against correct dataset version

**Code Changes**:
```python
# Get next version for this language
cursor.execute(
    "SELECT MAX(version) as max_version FROM gold_datasets 
     WHERE language_id = %s AND is_deleted = FALSE",
    (language_id,)
)
next_version = (result['max_version'] or 0) + 1

# Save with version in filename
file_path = lang_dir / f"v{next_version}_{timestamp}_{file.filename}"

# Insert with version
cursor.execute(
    "INSERT INTO gold_datasets (..., version) VALUES (..., %s)",
    (..., next_version)
)
```

### 4. **Filtering Deleted Items from UI** ✅

**Admin Dashboard**:
- Queries updated to `WHERE is_deleted = FALSE`
- Displays only active languages and datasets
- Admin can see archived items by querying database directly

**Leaderboards** (Homepage & Client Dashboard):
- Queries filter: `WHERE l.is_deleted = FALSE AND gd.is_deleted = FALSE`
- Only evaluations for latest dataset version shown: `gd.version = (SELECT MAX(version) ...)`
- No archived languages or datasets visible to users

**Example Query**:
```sql
SELECT ue.* FROM user_evaluations ue
JOIN users u ON ue.user_id = u.id
JOIN languages l ON ue.language_id = l.id
JOIN gold_datasets gd ON ue.language_id = gd.language_id
WHERE ue.language_id = ?
AND u.is_active = 1
AND gd.is_deleted = 0
AND gd.version = (
    SELECT MAX(version) FROM gold_datasets 
    WHERE language_id = ? AND is_deleted = 0
)
```

### 5. **Demo Storage Updates** ✅

**DEMO_LANGUAGES**:
- Added `is_deleted` flag to demo language objects
- Deleted languages filtered out when loading leaderboards

**DEMO_GOLD_DATASETS**:
- Added `is_deleted` and `version` fields
- Soft delete logic mirrors database behavior

## User Interface Changes

### Delete Buttons Redesigned
- **Old Text**: "Delete" with message "Are you sure you want to delete?"
- **New Text**: "Archive" with message "Archive this {item}? The data will be kept in the database but hidden from the dashboard."

### Confirmation Messages
**For Languages**:
```
Archive the language "Hindi"? This will also archive all associated gold datasets. 
The data will be kept in the database but hidden from the dashboard.
```

**For Datasets**:
```
Archive this gold dataset? The data will be kept in the database but hidden from the 
dashboard. Related evaluations will also be hidden.
```

## Database Schema Updates

### languages table
```sql
ALTER TABLE languages ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE;
ALTER TABLE languages ADD INDEX idx_is_deleted (is_deleted);
```

### gold_datasets table
```sql
ALTER TABLE gold_datasets ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE;
ALTER TABLE gold_datasets ADD COLUMN version INT DEFAULT 1;
ALTER TABLE gold_datasets ADD INDEX idx_is_deleted (is_deleted);
ALTER TABLE gold_datasets ADD INDEX idx_version (version);
```

## Migration Path

1. Run `migration.sql` - adds new columns with default values
2. Existing data automatically gets `is_deleted = FALSE` and `version = 1`
3. No data loss - all existing records preserved
4. System ready for new versioning behavior immediately

## Data Recovery

If a language or dataset is archived by mistake:

**SQL Recovery**:
```sql
-- Restore archived language
UPDATE languages SET is_deleted = FALSE WHERE id = 5;

-- Restore archived datasets for that language
UPDATE gold_datasets SET is_deleted = FALSE WHERE language_id = 5;
```

**Note**: Full recovery possible because data is never physically deleted from database.

## Testing Scenarios

### Scenario 1: Archive Language
1. Admin goes to Language Management
2. Clicks "Archive" on Hindi language
3. Confirmation dialog: "Archive the language 'Hindi'? This will also archive all associated gold datasets..."
4. After confirmation: Language and datasets marked as deleted
5. Homepage leaderboard no longer shows Hindi scores
6. Admin dashboard no longer shows Hindi in language list
7. Evaluations for Hindi still in database, just hidden from UI

### Scenario 2: Upload Multiple Dataset Versions
1. Admin uploads `dataset_v1.txt` for Hindi → saved as `v1_20251111_120530_dataset_v1.txt`, version=1
2. Later uploads `dataset_v2.txt` for same language → saved as `v2_20251111_120545_dataset_v2.txt`, version=2
3. New evaluations automatically use version=2
4. Old evaluations still reference version=1 data
5. Leaderboard only shows scores for version=2 evaluations

### Scenario 3: Archive Old Dataset Version
1. Admin has v1 and v2 of Hindi dataset
2. Admin clicks "Archive" on v1
3. v1 marked as `is_deleted = TRUE`
4. v2 still active
5. Leaderboard continues showing v2 scores
6. No disruption to current evaluations

## API Changes

### Affected Endpoints

**DELETE Operations** (Now Soft Delete):
- `POST /admin/delete_language/{language_id}` - marks language as deleted
- `POST /admin/delete_gold_dataset/{dataset_id}` - marks dataset as deleted

**GET Operations** (Filtering Updated):
- `GET /admin` - filters `is_deleted = FALSE`
- `GET /` (homepage) - filters deleted languages and datasets
- Leaderboard queries include version filtering

**POST Operations** (Versioning Added):
- `POST /admin/upload_gold_dataset` - auto-increments version, prefixes filename with `v{version}_`

## Performance Considerations

- **Indexes**: Added on `is_deleted` and `version` columns for fast filtering
- **Query Impact**: Additional WHERE clauses but well-indexed
- **Soft Delete Overhead**: Minimal - just boolean flag, no performance degradation
- **Version Lookup**: Single MAX(version) subquery - negligible impact

## Future Enhancements

1. **Restore Functionality**: Add UI button to restore archived items
2. **Audit Trail**: Track who archived what and when
3. **Permanent Delete**: After 90 days, hard delete archived items
4. **Version History**: Show all dataset versions and their evaluation counts
5. **Rollback**: Admin ability to revert to previous dataset version for evaluations
