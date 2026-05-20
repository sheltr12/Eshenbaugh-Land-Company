# Hillsborough County Vacant Land Comps

Sale window: Apr 19 2026 through May 19 2026.

Official sources used:
- Hillsborough County Property Appraiser `allsales_05_15_2026.zip`
- Hillsborough County Property Appraiser `parcel_05_15_2026.zip`
- Hillsborough County Property Appraiser `LatLon_Table_05_15_2026.zip`
- Hillsborough County hosted road centerline FeatureServer for nearest-street labels on `0`/blank site-address parcels

Criteria:
- `VI = V` in HCPA sales extract
- Sale price greater than $500,000
- Acreage greater than 0.5 acres from HCPA parcel extract
- Sale date from Apr 19 2026 through May 19 2026

Result count: 16 new comps.

Coordinate method:
Coordinates are from the official HCPA `LatLon_Table_05_15_2026` joined by folio. For parcels whose PA site address was `0`, blank-like, or not a usable physical address, display labels were replaced with `Near [street]` using the official county hosted road centerline service near the HCPA coordinate.

Important limitation:
The current ELC GitHub base files were not present in the workspace, so this package includes the extracted new-comp import and map, but not schema-preserving replacements for `comps.json`, `comps_with_aerials.json`, `comps_aerial_backup.json`, or the site files. Drop the latest GitHub files into the workspace and rerun the append step before uploading replacement database files.
