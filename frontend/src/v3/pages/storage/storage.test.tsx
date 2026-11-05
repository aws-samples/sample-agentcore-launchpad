import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { AgentInfo } from "../../../lib/api";
import type { AgentStorage, StorageEntry, StorageProvisionPlan } from "../../../lib/api/storage";
import { hostedGroups } from "../../nav";
import { StorageCardView } from "../agent/StorageCard";
import { ProvisionJobs } from "./ProvisionJobs";
import { ProvisionPlan } from "./ProvisionPlan";
import { StorageTable } from "./Storage";
import { StorageDetail } from "./StorageDetail";

const agent = { id: "a1", name: "lab", method: "container", status: "active", spec: {} } as unknown as AgentInfo;

const entry: StorageEntry = {
  id: "state",
  workspace_id: "default",
  name: "Pipeline state",
  kind: "s3files",
  access_point_arn: "arn:aws:s3files:us-west-2:111122223333:file-system/fs-1/access-point/fsap-1",
  file_system_arn: "arn:aws:s3files:us-west-2:111122223333:file-system/fs-1",
  bucket_arn: "arn:aws:s3:::state",
  posix_uid: 1000,
  posix_gid: 1000,
  root_directory: "/state",
  mount_target_azs: ["usw2-az1", "usw2-az2"],
  mount_targets: [{ id: "mt-1", subnet_id: "subnet-a", availability_zone_id: "usw2-az1", vpc_id: "vpc-1", security_groups: ["sg-mt"], status: "available" }],
  vpc_id: "vpc-1",
  network_profile_id: null,
  max_mode: "rw",
  data_class: "shared_state",
  origin: "registered",
  status: "warn",
  check_results: [
    { check: "access_point", severity: "ok", message: "the access point is available" },
    { check: "mount_targets.vpc", severity: "warn", message: "mount targets sit in several VPCs" },
  ],
  checked_at: null,
  created_by: null,
  created_at: null,
  updated_at: null,
  archived_at: null,
  allowed: true,
  projects: ["risk", "collect"],
  agents: [
    { agent_id: "a1", name: "lab", project_id: "risk", version: "3", status: "active", endpoint_mode: "live", mode: "rw", mount_path: "/mnt/state" },
    { agent_id: "a2", name: "etl", project_id: "collect", version: "1", status: "active", endpoint_mode: "default", mode: "rw", mount_path: "/mnt/state" },
  ],
  writers: 2,
  multi_writer: true,
};

const render = (node: JSX.Element) => renderToStaticMarkup(<MemoryRouter>{node}</MemoryRouter>);

describe("storage page", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  it("lists an entry with its check counts and the several-writers chip", () => {
    const html = render(<StorageTable entries={[entry]} selectedId="" busyId={null} onSelect={() => {}} onCheck={() => {}} onEdit={() => {}} onArchive={() => {}} />);
    expect(html).toContain("Pipeline state");
    expect(html).toContain("usw2-az1, usw2-az2");
    expect(html).toContain("read-write · shared state");
    expect(html).toContain("0 blocking · 1 warnings");
    expect(html).toContain("2 project(s) · 2 agent(s)");
    expect(html).toContain("several writers");
  });

  it("shows the detail with the check rows and the shared view", () => {
    const html = render(<StorageDetail entry={entry} />);
    expect(html).toContain("mount targets sit in several VPCs");
    expect(html).toContain("1000:1000 · /state");
    expect(html).toContain("usw2-az1 (subnet-a)");
    expect(html).toContain("Allowed for: risk, collect");
    expect(html).toContain("2 agents mount this storage read-write");
    expect(html).toContain("etl");
  });

  it("marks a console-made entry and offers its teardown", () => {
    const made = { ...entry, origin: "platform" as const, agents: [] };
    const html = render(<StorageTable entries={[made]} selectedId="" busyId={null} onSelect={() => {}} onCheck={() => {}}
      onEdit={() => {}} onArchive={() => {}} onTeardown={() => {}} />);
    expect(html).toContain("created here");
    expect(html).toContain("Delete the storage and its AWS resources");
    expect(render(<StorageTable entries={[{ ...made, origin: "registered" }]} selectedId="" busyId={null} onSelect={() => {}}
      onCheck={() => {}} onEdit={() => {}} onArchive={() => {}} onTeardown={() => {}} />)).not.toContain("created here");
  });

  it("follows a create stage by stage and offers a failed one's retry", () => {
    const html = render(<ProvisionJobs onRetry={() => {}} jobs={[{
      job_id: "j1", op: "create", storage_id: "datasets", status: "failed", done: true, resources: {}, request: {},
      stages: [
        { name: "bucket", status: "succeeded", detail: "created launchpad-s3files-1-abc" },
        { name: "mount-targets", status: "failed", detail: "no free IPs" },
      ],
      error: "mount-targets: no free IPs", created_at: null, updated_at: null,
    }]} />);
    expect(html).toContain("Mount targets");
    expect(html).toContain("created launchpad-s3files-1-abc");
    expect(html).toContain("mount-targets: no free IPs");
    expect(html).toContain("Retry");
    expect(render(<ProvisionJobs onRetry={() => {}} jobs={[]} />)).toBe("");
  });

  const plan: StorageProvisionPlan = {
    network_profile_id: "np-1",
    vpc_id: "vpc-1",
    vpc_cidrs: ["10.0.0.0/16"],
    mount_target_security_group: {
      name: "launchpad-mt-<id>", vpc_id: "vpc-1",
      inbound: [{ protocol: "tcp", port: 2049, source_group_id: "sg-agents" }], outbound: [],
    },
    mount_targets: [{ subnet_id: "subnet-a", availability_zone_id: "usw2-az1" }],
    agent_security_groups: [
      { group_id: "sg-agents", name: "agents", status: "missing",
        required_outbound: { protocol: "tcp", port: 2049, destination: "launchpad-mt-<id>" } },
    ],
    s3_gateway_endpoint: {
      service_name: "com.amazonaws.us-west-2.s3", endpoint_ids: [], status: "missing",
      unrouted_subnets: ["subnet-a"], docs: "https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-s3.html",
    },
    nat_gateways: ["nat-1"],
  };

  it("shows the new group, its VPC and rules, and what the administrator must add", () => {
    const html = render(<ProvisionPlan plan={plan} storageId="datasets" />);
    expect(html).toContain("launchpad-mt-datasets");
    expect(html).toContain("vpc-1 (10.0.0.0/16)");
    expect(html).toContain("Inbound");
    expect(html).toContain("sg-agents");
    expect(html).toContain("The allow-all rule AWS adds to a new group is removed");
    expect(html).toContain("subnet-a (usw2-az1)");
    expect(html).toContain("You need to add it");
    expect(html).toContain("Add an outbound rule to sg-agents");
    expect(html).toContain("add an S3 gateway endpoint");
    expect(html).toContain("com.amazonaws.us-west-2.s3");
    const ok = render(<ProvisionPlan storageId="" plan={{
      ...plan,
      agent_security_groups: [{ ...plan.agent_security_groups[0], status: "ok" }],
      s3_gateway_endpoint: { ...plan.s3_gateway_endpoint, status: "ok", endpoint_ids: ["vpce-1"], unrouted_subnets: [] },
    }} />);
    expect(ok).toContain("launchpad-mt-&lt;id&gt;");
    expect(ok).not.toContain("Add an outbound rule");
    expect(ok).toContain("S3 gateway endpoint (vpce-1)");
  });

  it("is an administrator's Configure entry after Network profiles", () => {
    const admin = hostedGroups(true).flatMap((g) => g.items.map((i) => i.to));
    expect(admin.indexOf("/v3/storage")).toBe(admin.indexOf("/v3/network") + 1);
    expect(hostedGroups(false).flatMap((g) => g.items.map((i) => i.to))).not.toContain("/v3/storage");
  });
});

describe("agent storage card", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  const data: AgentStorage = {
    agent_id: "a1",
    version: "3",
    session_storage: "/mnt/workspace",
    mounts: [
      { storage_id: "state", mount_path: "/mnt/state", mode: "rw", missing: false,
        entry: { name: "Pipeline state", kind: "s3files", status: "blocked", max_mode: "rw", data_class: "shared_state", archived: false, checked_at: null, blocking: 1, warnings: 0 } },
      { storage_id: "gone", mount_path: "/mnt/gone", mode: "ro", missing: true, entry: null },
    ],
    inline: [{ kind: "efs", access_point_arn: "arn:aws:elasticfilesystem:us-west-2:1:access-point/fsap-1", mount_path: "/mnt/raw", mode: "rw" }],
  };

  it("lists catalog and inline mounts with the change link", () => {
    const html = render(<StorageCardView agent={agent} loading={false} error={null} data={data} />);
    expect(html).toContain("Pipeline state");
    expect(html).toContain("1 blocking");
    expect(html).toContain("not in the catalog");
    expect(html).toContain("/mnt/raw");
    expect(html).toContain("cannot be promoted");
    expect(html).toContain("/v2/agents?view=edit&amp;id=a1");
  });

  it("lists a mount added outside Launchpad, read back from AWS", () => {
    const html = render(<StorageCardView agent={agent} loading={false} error={null} data={{
      ...data, mounts: [], inline: [],
      live: { status: "ok", kind: "runtime", entries: [
        { kind: "s3files", mount_path: "/mnt/data", access_point_arn: "arn:a", origin: "platform", storage_id: null, storage_name: null },
        { kind: "efs", mount_path: "/mnt/console", access_point_arn: "arn:b", origin: "external", storage_id: "shared", storage_name: "Shared" },
      ] },
    }} />);
    expect(html).toContain("/mnt/console");
    expect(html).toContain("added outside Launchpad");
    expect(html).toContain("Shared");
    expect(html).toContain("EFS");
    expect(html).toContain("A re-publish from Launchpad keeps them");
    expect(html).not.toContain("/mnt/data");
    const down = render(<StorageCardView agent={agent} loading={false} error={null}
      data={{ ...data, mounts: [], inline: [], live: { status: "unavailable", error: "AccessDenied", entries: [] } }} />);
    expect(down).toContain("Could not read the mounts on AWS (AccessDenied)");
  });

  it("renders nothing when only session storage is mounted", () => {
    expect(render(<StorageCardView agent={agent} loading={false} error={null} data={{ ...data, mounts: [], inline: [] }} />)).toBe("");
  });
});
