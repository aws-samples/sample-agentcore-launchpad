import { afterEach, describe, expect, it, vi } from "vitest";

import {
  bucketUri,
  type CatalogMountRow,
  catalogMountsSpec,
  catalogRowsFromSpec,
  countChecks,
  emptyStorageForm,
  maxMounts,
  modeAllowed,
  mountedPrefixHint,
  newCatalogRow,
  normalizePrefix,
  pickable,
  rowNeedsVpc,
  type StorageEntry,
  storageApi,
  storageFormToBody,
  storageKindOfArn,
  validateStorage,
  emptyProvisionForm,
  provisionFormToBody,
  type StorageProvisionJob,
  validateProvision,
  visibleJobs,
} from "./storage";

const S3_AP = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-0123456789abcdef0/access-point/fsap-0123456789abcdef0";
const EFS_AP = "arn:aws:elasticfilesystem:us-west-2:111122223333:access-point/fsap-0aa";

const entry = (id: string, fields: Partial<StorageEntry> = {}) => ({ id, status: "ok", archived_at: null, max_mode: "ro", ...fields }) as StorageEntry;

describe("catalog picks", () => {
  it("default to read-only under /mnt/<id> and round-trip through the spec", () => {
    const rows: CatalogMountRow[] = [newCatalogRow("datasets"), { storageId: "state", path: "/mnt/s", mode: "rw" }];
    expect(rows[0]).toEqual({ storageId: "datasets", path: "/mnt/datasets", mode: "ro" });
    const spec = catalogMountsSpec(rows);
    expect(spec).toEqual([
      { storage_id: "datasets", mount_path: "/mnt/datasets", mode: "ro" },
      { storage_id: "state", mount_path: "/mnt/s", mode: "rw" },
    ]);
    expect(catalogRowsFromSpec({ mounts: [...spec, { mount_path: "/mnt/x" }] })).toEqual(rows);
    expect(catalogRowsFromSpec(undefined)).toEqual([]);
  });

  it("offers only live, unblocked, unpicked entries and caps the mode", () => {
    const entries = [entry("a"), entry("b", { status: "blocked" }), entry("c", { archived_at: "x" }), entry("d", { max_mode: "rw" })];
    expect(pickable(entries, [newCatalogRow("d")]).map((e) => e.id)).toEqual(["a"]);
    expect(modeAllowed({ storageId: "a", path: "", mode: "rw" }, entries[0])).toBe(false);
    expect(modeAllowed({ storageId: "d", path: "", mode: "rw" }, entries[3])).toBe(true);
  });
});

describe("register form", () => {
  it("validates the id and the ARN when creating only", () => {
    expect(storageKindOfArn(S3_AP)).toBe("s3files");
    expect(storageKindOfArn(EFS_AP)).toBe("efs");
    expect(storageKindOfArn("arn:aws:s3:::bucket")).toBeNull();
    const bad = { ...emptyStorageForm(), id: "Data Sets", access_point_arn: "nope" };
    expect(validateStorage(bad, true)).toEqual({ id: "v3.storage.form.errId", access_point_arn: "v3.storage.form.errArn" });
    expect(validateStorage(bad, false)).toEqual({});
    expect(storageFormToBody({ ...emptyStorageForm(), id: " datasets ", access_point_arn: S3_AP })).toEqual({
      id: "datasets", name: undefined, access_point_arn: S3_AP, max_mode: "ro", data_class: "reference", network_profile_id: null,
    });
  });

  it("counts blocking and warning checks", () => {
    expect(countChecks([
      { check: "a", severity: "block", message: "" },
      { check: "b", severity: "warn", message: "" },
      { check: "c", severity: "info", message: "" },
    ])).toEqual({ block: 1, warn: 1 });
  });
});

describe("storage API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("calls the catalog routes", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ storage: [], status: "ok", results: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await storageApi.check("datasets");
    await storageApi.setForProject("alpha", ["datasets"]);
    await storageApi.preflight({ mounts: [{ storage_id: "datasets" }], network_profile_id: "np-1" });
    await storageApi.forAgent("a1");
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map(([url, init]) => `${init.method ?? "GET"} ${url}`)).toEqual([
      "POST /api/storage-mounts/datasets/check",
      "PUT /api/storage-mounts/projects/alpha",
      "POST /api/storage-mounts/preflight",
      "GET /api/storage-mounts/agents/a1",
    ]);
    expect(JSON.parse(String(calls[1][1].body))).toEqual({ storage_ids: ["datasets"] });
  });
});

describe("the console's S3 Files creation (storage-mounts §7)", () => {
  const job = (over: Partial<StorageProvisionJob>): StorageProvisionJob => ({
    job_id: "j1", op: "create", storage_id: "datasets", status: "running", done: false,
    stages: [], resources: {}, request: {}, error: null, created_at: null, updated_at: null, ...over,
  });

  it("validates the form like the server's ProvisionBody", () => {
    const ok = { ...emptyProvisionForm(), id: "datasets", network_profile_id: "np-1", root_directory: "/data/a" };
    expect(validateProvision(ok)).toEqual({});
    const bad = validateProvision({ ...ok, id: "Data", network_profile_id: "", posix_uid: "-1",
      posix_gid: "4294967296", root_directory: "data" });
    expect(Object.keys(bad).sort()).toEqual(["id", "network_profile_id", "posix_gid", "posix_uid", "root_directory"]);
    expect(validateProvision({ ...ok, root_directory: "/a/b/c/d/e" }).root_directory).toBe("v3.storage.provision.errRoot");
    // the file system root belongs to root: an access point there cannot write
    expect(validateProvision({ ...ok, root_directory: "/" }).root_directory).toBe("v3.storage.provision.errRoot");
  });

  it("sends numbers and drops an empty name", () => {
    const body = provisionFormToBody({ ...emptyProvisionForm(), id: " datasets ", network_profile_id: "np-1", posix_uid: "0" });
    expect(body).toEqual({ id: "datasets", name: undefined, network_profile_id: "np-1", max_mode: "ro",
      data_class: "reference", posix_uid: 0, posix_gid: 1000, root_directory: "/data" });
  });

  it("shows running jobs and a failed latest one, once per storage", () => {
    const shown = visibleJobs([
      job({ job_id: "a", storage_id: "x" }),
      job({ job_id: "b", storage_id: "y", status: "failed", done: true }),
      job({ job_id: "c", storage_id: "z", status: "succeeded", done: true }),
      job({ job_id: "d", storage_id: "x", status: "failed", done: true }),
    ]);
    expect(shown.map((j) => j.job_id)).toEqual(["a", "b"]);
  });
});

describe("S3 bucket entries", () => {
  const bucketForm = () => ({ ...emptyStorageForm(), id: "reports", kind: "s3bucket" as const, bucket: "acme-data" });

  it("validate the bucket, prefix, owner and Region when creating", () => {
    expect(validateStorage(bucketForm(), true)).toEqual({});
    expect(validateStorage({ ...bucketForm(), bucket: "Bad_Bucket" }, true).bucket).toBe("v3.storage.form.errBucket");
    expect(validateStorage({ ...bucketForm(), bucket: "acme--use1-az4--x-s3" }, true).bucket).toBe("v3.storage.form.errBucket");
    for (const prefix of ["a/*", "a/{runtimeSessionId}/", "/lead", "a//b"]) {
      expect(validateStorage({ ...bucketForm(), prefix }, true).prefix).toBe("v3.storage.form.errPrefix");
    }
    expect(validateStorage({ ...bucketForm(), prefix: "team/out" }, true)).toEqual({});
    expect(validateStorage({ ...bucketForm(), bucket_owner: "12" }, true).bucket_owner).toBe("v3.storage.form.errOwner");
    expect(validateStorage({ ...bucketForm(), bucket_region: "mars" }, true).bucket_region).toBe("v3.storage.form.errRegion");
    // an access point ARN is not asked of a bucket entry, nor a bucket of an access point
    expect(validateStorage({ ...bucketForm(), access_point_arn: "" }, true).access_point_arn).toBeUndefined();
    expect(validateStorage({ ...emptyStorageForm(), id: "datasets", access_point_arn: S3_AP }, true)).toEqual({});
    expect(validateStorage({ ...bucketForm(), bucket: "" }, false)).toEqual({});
  });

  it("send the bucket fields, a normalized prefix and no access point", () => {
    expect(storageFormToBody({ ...bucketForm(), prefix: " team/out ", scope: "shared", bucket_owner: "444455556666", max_mode: "rw" })).toEqual({
      id: "reports", name: undefined, kind: "s3bucket", bucket: "acme-data", prefix: "team/out/", scope: "shared",
      bucket_owner: "444455556666", bucket_region: undefined, max_mode: "rw", data_class: "reference", network_profile_id: null,
    });
    expect(normalizePrefix("")).toBe("");
    expect(normalizePrefix("a/")).toBe("a/");
  });

  it("show where an agent's files land", () => {
    const e = { bucket: "acme-data", prefix: "team/", scope: "per_session" as const };
    expect(bucketUri(e)).toBe("s3://acme-data/team/");
    expect(mountedPrefixHint(e)).toBe("s3://acme-data/team/<agent_id>/<session_id>/");
    expect(mountedPrefixHint({ ...e, scope: "shared" })).toBe("s3://acme-data/team/");
    expect(bucketUri({ bucket: "acme-data", prefix: "" })).toBe("s3://acme-data/");
  });

  it("carry their kind through a pick and a stored spec, and need no VPC", () => {
    const row = newCatalogRow("reports", "s3bucket");
    expect(row).toEqual({ storageId: "reports", path: "/mnt/reports", mode: "ro", kind: "s3bucket" });
    expect(rowNeedsVpc(row)).toBe(false);
    expect(rowNeedsVpc(newCatalogRow("lake", "s3files"))).toBe(true);
    expect(rowNeedsVpc(newCatalogRow("lake"))).toBe(true); // unknown kind: the safe side
    expect(catalogRowsFromSpec({ mounts: [{ storage_id: "reports", mount_path: "/mnt/reports", mode: "rw", kind: "s3bucket" }] }))
      .toEqual([{ storageId: "reports", path: "/mnt/reports", mode: "rw", kind: "s3bucket" }]);
    // the kind is the server's stamp: it is not sent back
    expect(catalogMountsSpec([row])).toEqual([{ storage_id: "reports", mount_path: "/mnt/reports", mode: "ro" }]);
  });

  it("count every filesystem entry against AgentCore's five", () => {
    // managed session storage and the S3 output folder each take one entry
    expect(maxMounts(0)).toBe(5);
    expect(maxMounts(1)).toBe(4);
    expect(maxMounts(2)).toBe(3);
  });
});
