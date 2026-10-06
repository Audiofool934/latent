# Editing archived photos

This workflow is available in the native macOS client.
The web client continues to provide its existing library and Sequence workflows.
Use **Locations** in the sidebar or Command-comma to choose filesystem folders.
Local disks, external drives, and CloudDrive2 mounts use the same folder interface.
CloudDrive2 is only needed for a cloud mount or the legacy CloudDrive archive adapter.
ExifTool is needed for ARW embedded previews and writing working-copy XMP metadata.

## Choose locations

Add an original photo folder, then choose **Scan for photos** to build local previews.
ARW, JPEG, PNG, and TIFF are indexed; a folder scan does not submit images to an AI provider.
New photos need a separate Gemini embedding build before they participate in semantic search.
The existing library remains available when adding a source, including its ratings and Sequences.
For the existing CloudDrive archive, use **Connect archive folder** and choose the folder corresponding exactly to the displayed archive root.
Binding that root preserves existing asset IDs, cached previews, and fingerprints instead of creating a second archive.
**Reconnect folder** keeps a source identity when its drive or mount path changes and checks representative indexed file paths and sizes.
It is a hierarchy check, not a full content verification of the archive.
CloudDrive2 folders also retain the provider's stable folder ID, because their local device and inode numbers can change after a remount.
The same confirmed cloud folder remains usable after that change; a different cloud folder, an absent mount, or a replaced local disk still stops access.
Previously connected folders need one reconnect to record this stable identity.

Choose a separate **Finished photos** output folder, such as a folder on the Google Drive mount.
For an original at `2025/2025-12-12/DSC08490.ARW`, an export named `DSC08490.jpg` is saved under `2025/2025-12-12/DSC08490.jpg` in that output folder.
The export's filename is preserved and its parent directories come from the associated original.
If output equals an input root or contains one, finished photos use an `_Latent Edits` subtree.
Output subtrees, previous configured output subtrees, working-copy folders, the configured Sequence projection, and `.Latent Trash` are excluded from source scans.
Scans do not follow symbolic links.
Existing output files are never replaced: identical content can be reused, while a different version gets a hash suffix.

## Edit and save selected photos

1. Select photos from the library, Starred, search, or a Sequence using Cmd-click or Shift-click.
2. Choose **Edit in PhotoLab** and wait for the working copies to finish downloading or copying.
3. Edit them in PhotoLab and export finished JPEG, HEIC, TIFF, or PNG files into the working folder or a subfolder.
4. Save PhotoLab `.dop` sidecars to keep the editing settings locally.
5. Return to **Editing** and choose **Review finished photos**.
6. Review each original-photo association and destination; renamed exports can be matched using the original-photo picker.
7. Select only the finished photos to save, then choose **Save photos to output folder**.

Only selected finished photos are copied to the output.
All working originals, sidecars, and exports remain in the working folder.
The completed status confirms a checksummed filesystem copy; CloudDrive2 is responsible for its subsequent cloud upload.
Check CloudDrive2 before relying on the remote copy.
A reviewed batch captures its output folder, so changing Locations does not silently redirect it.
Use **Refresh files** for a new review if you intend to change its destination.

Working folders are under `~/Pictures/Latent/<date>-<short-batch-id>/` by default.
The service option `--editing-dir` changes the folder for future batches.
Each manifest records its batch's exact working folder, so restarting or changing the option preserves existing edits.
All selected originals share a folder so PhotoLab can show one filmstrip.
Duplicate filenames and stems receive a prefix in working copies, avoiding both original and XMP name collisions.
The manifest preserves each original's library-relative path.

## Sequence folders without duplicate originals

Choose a **Sequence links** parent folder in Locations, then use **Update sequence links**.
Latent creates `sequences/` beneath it and mirrors the user-defined folder hierarchy.
Each Sequence contains ordered, numbered symbolic links to its original photos.
Run the update after renaming, moving, reordering, or changing a Sequence.
Only previously recorded links with the expected target are replaced or removed; unrelated files are preserved.
A manifest identifies the library that owns the projection and prevents another workspace from taking it over.
Names that cannot be filesystem components are sanitized, and sibling name collisions receive stable identifiers.
Filesystem path-length limits still apply to deeply nested trees.

These links require the original disk or mount to be available at the configured path.
They are ordinary filesystem symbolic links and are not Google Drive web shortcuts.
The selected filesystem must support symbolic links.
A cloud service's handling of those links must be checked separately.
The local projection does not establish that a cloud provider will preserve or synchronize those links.

## Recovery and data preservation

Library browsing and saved Sequences continue to use local cached data while a source is offline.
Mutations require loopback access and a same-origin or native client.
Folder changes and editing actions check library identity and the reviewed configuration revision.
A missing or replaced root stops filesystem access; the service never recreates an absent mount root.
Reconnect the input folder or choose the output again, then refresh the affected review before retrying.

Originals are read through bounded ranges; legacy CloudDrive reads retain the existing HTTP-range adapter until a mounted archive folder is connected.
Downloads check the indexed fingerprint, complete byte counts, and available cloud content hashes.
Finished photos are staged in hidden temporary files and published without replacing an existing destination, then read back for SHA-256 verification.
An abrupt process crash can leave a hidden `.latent-*.partial` staging file, but cannot publish an incomplete final filename.
Interrupted transfers preserve their manifests and keep all working files.
Only one editing transfer runs at a time.

Existing archive `.dop` and `.xmp` sidecars accompany the original.
When editing again, a saved filesystem batch restores locally retained sidecars.
Explicit Latent ratings and captions are written into working XMP without changing the original archive bytes.
PhotoLab may require **Metadata > Read from image** for photos already known to its database.
Latent does not change PhotoLab's global filters or synchronization preferences.

Back up the workspace directory, including `locations.json` and `editing/<batch-id>/manifest.json`, plus the working folders in Pictures.
Workspace JSON exports contain annotations and Sequences but do not embed location configuration, batch manifests, or image files.
Previously prepared or interrupted legacy CloudDrive uploads retain their original destination and verified cleanup policy so they can be resumed deliberately.
New completion reviews use the selected filesystem output and keep all local working files.

## Legacy CloudDrive hash verification

CloudDrive can finish accepting an upload before the provider reports its content hash.
The old completion path checked once and could label that temporary state as an error even when the file later uploaded successfully.
The service now checks repeatedly for up to 90 seconds and reports **Waiting for cloud confirmation** when hashes are still unavailable.
Local files remain intact while confirmation is pending.

For an existing upload batch, **Verify cloud copies** checks only its recorded uploaded paths and expected hashes.
It does not upload again or remove local files, including when the batch previously selected a cleanup policy.
A successful check records **Cloud copies verified** durably so reopening Editing does not show the old error.
New completion reviews continue to use the selected filesystem output workflow described above.

## Delete and restore originals

The native gallery's Trash action moves selected originals into `.Latent Trash/<batch-id>/` under their connected input root, preserving the relative folder hierarchy.
For the existing CloudDrive archive, connect the exact mounted root in Locations first.
Latent verifies legacy CloudDrive content hashes before moving originals and uses a same-volume move that refuses to replace an existing file.
The sidebar Trash view restores a batch or resumes an interrupted operation.
Restoration refuses to overwrite a new file at the original path.
Trashed originals stay out of source scans and library results while annotations, cached previews, embeddings, and ordinary Sequence references remain available for restoration.
Editing copies, finished exports, and sidecars are retained.
Back up `workspace/trash/` with the workspace because its manifests associate hidden catalog entries with the moved originals.

## Timeline, filters, and Trash verification

The October 4 update passed the Python suite, focused filter and Trash checks, Ruff, and all 120 native tests.
The native UI run used three generated JPEGs on the actual CloudDrive2 aDrive mount with cloud-confirmed SHA-1 values.
It combined a Timeline date range, minimum rating, Pick status, and a deterministic semantic-query fixture, then saved those filters as a smart Sequence inside a folder.
Lowering a matching photo's rating removed it from that smart Sequence immediately; restoring the rating returned its membership.
The semantic UI fixture used a local test encoder rather than a new live Gemini request.

The native delete confirmation was cancelled once to prove that every original remained unchanged, then accepted for two generated photos.
CloudDrive's refreshed directory metadata confirmed both originals moved to Latent Trash with unchanged hashes.
Native Restore returned both originals; all three file hashes, ratings, ordinary Sequence references, smart membership, and embedding counts were checked again.
No user archive photo was deleted for this test.

The real interrupted editing batch was then verified through CloudDrive: all six uploaded sidecars and JPEGs matched their recorded cloud hashes.
The formal Editing sheet displayed **Cloud copies verified**, and all eight local RAW, sidecar, and JPEG files retained their original SHA-256 values.
The updated signed native app and port-8766 service use the existing library identity and all 25,793 catalog photos.
Evidence is retained under `var/timeline-search-trash-20261004/`.
The three generated cloud files and their temporary root were removed after verification, with refreshed CloudDrive metadata confirming that the root was absent.
The QA app and service exited; the formal native app and existing supervised library service remain running for use.

## Folder workflow verification

The final change passed 134 Python tests, Ruff, and 113 native tests, including the real Swift-to-Python service ownership test.
The isolated native run used three generated JPEG originals under `var/folder-workflow-qa-20261004/`.
It selected input, output, and Sequence parent folders through native file dialogs, scanned the originals, and exported two of three finished photos through the actual Editing sheet.
The output contained exactly the selected two photos in their original relative directory, while every working original and sidecar remained.
The Sequence action produced three real symbolic links under `sequences/Travel/Mountains/Final selection/`, all resolving to the intended originals.
Rendered Locations and Editing sheets were inspected for readability and layout.
The final native build also preserved export selections across original-photo remapping, reopened its saved location configuration, and safely saved the same two exports again without creating duplicates.
Automated tests cover overlapping roots, offline or replaced folders, output conflicts, changed files after review, explicit renamed-export mapping, a frozen output destination, legacy identity preservation, working-stem collisions, and preservation of unmanaged Sequence files.
Real cloud upload completion and symbolic-link behavior on a CloudDrive2 Google Drive mount remain unverified because no live output destination was selected.
The signed application and port-8766 service were updated with the same library identity.
All 25,793 asset IDs and fingerprints, the complete Gemini index, the existing editing manifest, and hashes of every working file were preserved.
The task-owned QA app and test server were stopped after verification.

## Earlier PhotoLab and Pictures verification

The October 4 location update passed 116 Python tests, Ruff, and all 113 native tests in the actual repository checkout.
Tests cover a working folder outside the workspace, restart after completion review, changes to the destination for future batches, verified cleanup, sidecar restoration, and legacy manifests both before and after an explicit relocation.
The live ready batch was moved to `~/Pictures/Latent/2026-10-04-98abb2ed/` while PhotoLab and the library service were closed; both RAWs and all four sidecars retained identical hashes.
The batch ID, transfer state, and library identity were preserved.
After activating the signed app and matching service, the Editing panel displayed the new folder and PhotoLab selected it with both images in its filmstrip.
The previous individual-RAW open request had left PhotoLab at `0/0 images`; sending the folder fixed this observed handoff.
Migration receipts and current test logs are under `var/pictures-editing-20261004/`.

Automated checks exercise workspace migration and annotation persistence, HTTP annotation and Starred behavior, cross-origin rejection, complete and interrupted downloads, duplicate basenames, XMP writes, pending cloud verification, retained JPEGs, cleanup recovery, changed files after review, and restoring prior sidecars.
Swift checks cover the native client's existing navigation, geometry, image-loading, and request contracts.

The isolated native UI check used `var/editing-verification/workspace` and port 8877.
It exercised multiple selection, keyboard star ratings, caption save, Starred, persistence after restart, a real 77,541,376-byte CloudDrive RAW download, and a completion review containing PhotoLab's real `.dop`, `.xmp`, and 2048 x 1365 JPEG export.
The downloaded RAW matched the archive SHA-1, and the exported JPEG retained four stars.
PhotoLab required its existing four-star filter to be cleared for the first import and then an explicit metadata read for this already-opened test photo.
Live cloud upload and cleanup have not yet been exercised against the real archive; those paths currently have controlled test coverage.
