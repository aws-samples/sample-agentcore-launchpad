import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type CatalogMountRow,
  catalogMountsSpec,
  catalogRowsFromSpec,
  countChecks,
  emptyStorageForm,
  modeAllowed,
  newCatalogRow,
  pickable,
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
