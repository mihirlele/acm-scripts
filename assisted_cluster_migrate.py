#!/usr/bin/env python3
"""
Unified backup/restore tool for Assisted Installer–managed clusters on ACM that allows to migrate ACM installed clusters from one ACM hub to another ACM hub

Usage examples:

  # Backup (export)
  ./assisted_cluster_migrate.py --mode backup --cluster baremetal-ocp \
    --output-dir /tmp/baremetal-ocp-backup

  # Restore (import) onto a (possibly different) hub
  ./assisted_cluster_migrate.py --mode restore --cluster baremetal-ocp \
    --backup-dir /tmp/baremetal-ocp-backup


BACKUP (cluster already installed):
  - Validates:
      * At least one InfraEnv exists where:
            spec.clusterRef.name      == <cluster>
            spec.clusterRef.namespace == <cluster>
      * All BareMetalHosts (if any) in any InfraEnv namespace
        are in 'provisioned' state.
      * ClusterDeployment.spec.installed == true.
      * ManagedCluster is Available=True.
      * Required CRs (in cluster namespace):
            AgentClusterInstall (name=<cluster>)
            KlusterletAddonConfig (name=<cluster>)
      * Required secrets in <cluster> namespace:
            - hive.openshift.io/secret-type=kubeconfig
            - hive.openshift.io/secret-type=kubeadmincreds
            - app.kubernetes.io/instance=clusters,
              agent-install.openshift.io/watch=true
  - Backs up:
      * Core CRs (cluster namespace):
            AgentClusterInstall, ClusterDeployment,
            ManagedCluster, KlusterletAddonConfig
      * ALL InfraEnvs whose spec.clusterRef matches the cluster:
            manifests/infraenvs/<ns>-<name>.yaml
      * For each InfraEnv namespace:
            - Agent CRs whose spec.clusterDeploymentName matches the cluster
                  manifests/agents/<ns>-<agentname>.yaml
            - BareMetalHosts (optional)
                  manifests/baremetalhosts/<ns>-<bmhname>.yaml
            - NmstateConfigs (optional)
                  manifests/nmstateconfigs/<ns>-<nmname>.yaml
      * Secrets in cluster namespace:
            - Admin kubeconfig secret:
                  secrets/AdminKubeconfigSecret.yaml
            - Kubeadmin credentials:
                  secrets/kubeadmincreds/*.yaml
            - Pull secret:
                  secrets/pullsecrets/*.yaml
            - BMC secrets (optional):
                  secrets/bmc/*.yaml
  - Cleans ownerReferences ONLY from:
      * manifests/AgentClusterInstall.yaml
      * secrets/AdminKubeconfigSecret.yaml
      * secrets/kubeadmincreds/*.yaml
      * secrets/bmc/*.yaml
  - After backup completes, prints ONLY:
      * oc patch ClusterDeployment preserveOnDelete
      * oc delete ManagedCluster


RESTORE:
  - Strict pre-restore checks:
      * 'assisted-service' and 'agentinstalladmission' deployments READY
        in namespace 'multicluster-engine'.
      * If backup contains any BareMetalHost manifests:
            manifests/baremetalhosts/*.yaml
        then require:
            metal3, metal3-baremetal-operator,
            metal3-image-customization
        deployments READY in 'openshift-machine-api'.
  - Ensures all namespaces required by the restored YAMLs exist:
      * Reads metadata.namespace from each YAML
      * Creates the namespace if missing
      * **No namespace rewriting**: original namespaces are preserved.
  - Restores in order:
      * Secrets: kubeconfig, kubeadmincreds, pullsecret, optional BMC.
      * Core CRs: ClusterDeployment, AgentClusterInstall, KlusterletAddonConfig, ManagedCluster.
      * InfraEnvs (all matching ones, in their original namespaces).
      * Agents (in their InfraEnv namespaces).
      * optional NmstateConfigs.
      * optional BareMetalHosts.
  - After restore completes: prints only a simple “RESTORE COMPLETE” banner.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import yaml


# --------------------------------------------------------------------------
# Common Utility Helpers
# --------------------------------------------------------------------------

def run_cmd(cmd, check=True):
    """Run a shell command and return output. Exit on failure if check=True."""
    result = subprocess.run(cmd, text=True, capture_output=True)
    if check and result.returncode != 0:
        print(f"\nERROR running: {' '.join(cmd)}", file=sys.stderr)
        if result.stdout:
            print("STDOUT:\n", result.stdout, file=sys.stderr)
        if result.stderr:
            print("STDERR:\n", result.stderr, file=sys.stderr)
        sys.exit(1)
    return result


def require_binary(name):
    """Ensure a binary exists in PATH."""
    if shutil.which(name) is None:
        print(f"ERROR: Required binary '{name}' not found in PATH.", file=sys.stderr)
        sys.exit(1)


def oc_get(kind, name=None, namespace=None, required=True):
    """oc get KIND [NAME] [-n NS] -o json -> parsed JSON."""
    cmd = ["oc", "get", kind]
    if name:
        cmd.append(name)
    if namespace:
        cmd += ["-n", namespace]
    cmd += ["-o", "json"]

    result = run_cmd(cmd, check=required)
    if not result.stdout.strip():
        if required:
            print(f"ERROR: No output for {kind}", file=sys.stderr)
            sys.exit(1)
        return {}
    return json.loads(result.stdout)


def oc_get_all(kind):
    """oc get KIND -A -o json -> parsed JSON."""
    cmd = ["oc", "get", kind, "-A", "-o", "json"]
    result = run_cmd(cmd)
    return json.loads(result.stdout)


def oc_get_by_label(kind, namespace, selector):
    """Return list of names for resources matching label selector."""
    cmd = ["oc", "get", kind, "-l", selector, "-o", "json"]
    if namespace:
        cmd += ["-n", namespace]

    result = run_cmd(cmd)
    data = json.loads(result.stdout)
    return [item["metadata"]["name"] for item in data.get("items", [])]


def dump_yaml(kind, name, filepath, namespace=None):
    """Dump a single resource to a YAML file."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    cmd = ["oc", "get", kind, name, "-o", "yaml"]
    if namespace:
        cmd += ["-n", namespace]

    print(f"  → Dumping {kind}/{name} -> {filepath}")
    result = run_cmd(cmd)
    with open(filepath, "w") as f:
        f.write(result.stdout)


# --------------------------------------------------------------------------
# BACKUP-SIDE DISCOVERY & VALIDATIONS
# --------------------------------------------------------------------------

def discover_infraenvs_for_cluster(cluster, cluster_ns):
    """
    Find all InfraEnvs with:
      spec.clusterRef.name      == cluster
      spec.clusterRef.namespace == cluster_ns
    Returns list of dicts: [{name, namespace}, ...]
    """
    print(f"Discovering InfraEnvs for clusterRef {cluster_ns}/{cluster} ...")
    data = oc_get_all("infraenv.agent-install.openshift.io")
    infraenvs = []

    for item in data.get("items", []):
        spec = item.get("spec", {})
        cref = spec.get("clusterRef", {})
        if (cref.get("name") == cluster
                and cref.get("namespace") == cluster_ns):
            infraenvs.append({
                "name": item["metadata"]["name"],
                "namespace": item["metadata"]["namespace"],
            })

    if not infraenvs:
        print(f"ERROR: No InfraEnv found for clusterRef="
              f"{cluster_ns}/{cluster}", file=sys.stderr)
        sys.exit(1)

    print(f"  OK: Found {len(infraenvs)} InfraEnv(s) "
          f"for clusterRef {cluster_ns}/{cluster}")
    for ie in infraenvs:
        print(f"    - {ie['namespace']}/{ie['name']}")
    return infraenvs


def validate_bmh_states(infraenvs):
    print("Checking BareMetalHost states (optional)...")
    namespaces = sorted({ie["namespace"] for ie in infraenvs})
    found_any = False
    not_prov = []

    for ns in namespaces:
        data = oc_get("baremetalhosts.metal3.io", namespace=ns, required=False)
        items = data.get("items", [])
        if not items:
            continue
        found_any = True
        for bmh in items:
            state = bmh.get("status", {}).get("provisioning", {}).get("state", "")
            name = f"{bmh['metadata'].get('namespace','default')}/{bmh['metadata']['name']}"
            if state.lower() != "provisioned":
                not_prov.append((name, state))

    if not_prov:
        print("ERROR: Some BareMetalHosts are NOT provisioned:", file=sys.stderr)
        for (name, state) in not_prov:
            print(f"  {name}: {state}", file=sys.stderr)
        sys.exit(1)

    if not found_any:
        print("  OK: No BareMetalHosts found across InfraEnv namespaces (optional).")
    else:
        print("  OK: All BareMetalHosts across InfraEnv namespaces are provisioned.")


def validate_clusterdeployment(cluster, ns):
    cd = oc_get("clusterdeployment.hive.openshift.io", cluster, ns)
    if not cd.get("spec", {}).get("installed", False):
        print("ERROR: ClusterDeployment.spec.installed != true", file=sys.stderr)
        sys.exit(1)
    print("  OK: ClusterDeployment installed=true")
    return cd


def validate_managedcluster_available(cluster):
    mc = oc_get("managedcluster.cluster.open-cluster-management.io", cluster)
    conds = mc.get("status", {}).get("conditions", [])
    avail = next((c for c in conds if c.get("type") == "ManagedClusterConditionAvailable"), None)
    if not avail or avail.get("status") != "True":
        print("ERROR: ManagedCluster NOT Available=True", file=sys.stderr)
        sys.exit(1)
    print("  OK: ManagedCluster Available=True")
    return mc


def validate_aci_kac(cluster, ns):
    oc_get("agentclusterinstall.extensions.hive.openshift.io", cluster, ns)
    oc_get("klusterletaddonconfig.agent.open-cluster-management.io", cluster, ns)
    print("  OK: AgentClusterInstall and KlusterletAddonConfig present")


def validate_secrets(cluster, ns):
    print("Checking mandatory secrets...")

    kubeconfig = oc_get_by_label("secret", ns, "hive.openshift.io/secret-type=kubeconfig")
    kubeadmin = oc_get_by_label("secret", ns, "hive.openshift.io/secret-type=kubeadmincreds")
#    pullsecret = oc_get_by_label(
#        "secret", ns, "app.kubernetes.io/instance=clusters,agent-install.openshift.io/watch=true"
#    )

    pullsecret = []

    watch_secrets = oc_get_by_label(
        "secret", ns, "agent-install.openshift.io/watch=true"
    )

    for sec in watch_secrets:
        sdata = oc_get("secret", sec, ns)
        if sdata.get("type") == "kubernetes.io/dockerconfigjson":
            pullsecret.append(sec)

    if not kubeconfig:
        print("ERROR: No kubeconfig secret found", file=sys.stderr)
        sys.exit(1)
    if not kubeadmin:
        print("ERROR: No kubeadmincreds secret found", file=sys.stderr)
        sys.exit(1)
    if not pullsecret:
        print("ERROR: No pullsecret found", file=sys.stderr)
        sys.exit(1)

    bmc = oc_get_by_label("secret", ns, "environment.metal3.io=baremetal")

    print("  OK: Mandatory secrets found")
    return {
        "kubeconfig": kubeconfig,
        "kubeadmin": kubeadmin,
        "pullsecret": pullsecret,
        "bmc": bmc,
    }


# --------------------------------------------------------------------------
# BACKUP LOGIC
# --------------------------------------------------------------------------

def backup_all(cluster, cluster_ns, backup_dir, infraenvs, secrets):
    print(f"\nCreating backup directory {backup_dir}")
    os.makedirs(backup_dir, exist_ok=True)

    mdir = os.path.join(backup_dir, "manifests")
    sdir = os.path.join(backup_dir, "secrets")
    os.makedirs(mdir, exist_ok=True)
    os.makedirs(sdir, exist_ok=True)

    # Core CRs (cluster namespace)
    print("\nDumping core CRs...")

    aci_path = os.path.join(mdir, "AgentClusterInstall.yaml")
    dump_yaml("agentclusterinstall.extensions.hive.openshift.io", cluster, aci_path, cluster_ns)

    dump_yaml("clusterdeployment.hive.openshift.io", cluster,
              os.path.join(mdir, "ClusterDeployment.yaml"), cluster_ns)
    dump_yaml("managedcluster.cluster.open-cluster-management.io", cluster,
              os.path.join(mdir, "ManagedCluster.yaml"))
    dump_yaml("klusterletaddonconfig.agent.open-cluster-management.io", cluster,
              os.path.join(mdir, "KlusterletAddonConfig.yaml"), cluster_ns)

    # InfraEnvs (multi-namespace)
    print("\nDumping InfraEnv CRs...")
    infra_dir = os.path.join(mdir, "infraenvs")
    os.makedirs(infra_dir, exist_ok=True)
    for ie in infraenvs:
        dump_yaml("infraenv.agent-install.openshift.io",
                  ie["name"],
                  os.path.join(infra_dir, f"{ie['namespace']}-{ie['name']}.yaml"),
                  ie["namespace"])

    # Agents per InfraEnv namespace (filtered by clusterDeploymentName)
    print("\nDumping Agent CRs...")
    agent_dir = os.path.join(mdir, "agents")
    os.makedirs(agent_dir, exist_ok=True)
    total_agents = 0
    processed_agent_ns = set()

    for ie in infraenvs:
        ns = ie["namespace"]
        if ns in processed_agent_ns:
            # Avoid scanning same namespace multiple times if multiple InfraEnvs share it
            continue
        processed_agent_ns.add(ns)

        data = oc_get("agent.agent-install.openshift.io", namespace=ns, required=False)
        for item in data.get("items", []):
            spec = item.get("spec", {})
            cdn = spec.get("clusterDeploymentName", {})
            cd_name = cdn.get("name")
            cd_ns = cdn.get("namespace")
            if cd_name != cluster or cd_ns != cluster_ns:
                continue

            aname = item["metadata"]["name"]
            dump_yaml(
                "agent.agent-install.openshift.io",
                aname,
                os.path.join(agent_dir, f"{ns}-{aname}.yaml"),
                ns,
            )
            total_agents += 1

    if total_agents == 0:
        print("ERROR: No Agent CRs found across InfraEnv namespaces "
              f"for clusterDeploymentName={cluster_ns}/{cluster}", file=sys.stderr)
        sys.exit(1)

    # NmstateConfigs per InfraEnv namespace (optional)
    print("\nDumping NmstateConfigs (optional)...")
    nm_dir = os.path.join(mdir, "nmstateconfigs")
    os.makedirs(nm_dir, exist_ok=True)
    processed_nm_ns = set()
    for ie in infraenvs:
        ns = ie["namespace"]
        if ns in processed_nm_ns:
            continue
        processed_nm_ns.add(ns)
        data = oc_get("nmstateconfig.agent-install.openshift.io", namespace=ns, required=False)
        for item in data.get("items", []):
            nname = item["metadata"]["name"]
            dump_yaml("nmstateconfig.agent-install.openshift.io",
                      nname,
                      os.path.join(nm_dir, f"{ns}-{nname}.yaml"),
                      ns)

    # BareMetalHosts per InfraEnv namespace (optional)
    print("\nDumping BareMetalHosts (optional)...")
    bmh_dir = os.path.join(mdir, "baremetalhosts")
    os.makedirs(bmh_dir, exist_ok=True)
    processed_bmh_ns = set()
    for ie in infraenvs:
        ns = ie["namespace"]
        if ns in processed_bmh_ns:
            continue
        processed_bmh_ns.add(ns)
        data = oc_get("baremetalhosts.metal3.io", namespace=ns, required=False)
        for item in data.get("items", []):
            bname = item["metadata"]["name"]
            dump_yaml("baremetalhosts.metal3.io",
                      bname,
                      os.path.join(bmh_dir, f"{ns}-{bname}.yaml"),
                      ns)

    # Secrets (cluster namespace)
    print("\nDumping secrets...")
    admin_kcfg = secrets["kubeconfig"][0]
    admin_kcfg_path = os.path.join(sdir, "AdminKubeconfigSecret.yaml")
    dump_yaml("secret", admin_kcfg, admin_kcfg_path, cluster_ns)

    ka_dir = os.path.join(sdir, "kubeadmincreds")
    os.makedirs(ka_dir, exist_ok=True)
    for sec in secrets["kubeadmin"]:
        dump_yaml("secret", sec, os.path.join(ka_dir, f"{sec}.yaml"), cluster_ns)

    ps_dir = os.path.join(sdir, "pullsecrets")
    os.makedirs(ps_dir, exist_ok=True)
    for sec in secrets["pullsecret"]:
        dump_yaml("secret", sec, os.path.join(ps_dir, f"{sec}.yaml"), cluster_ns)

    if secrets["bmc"]:
        bmc_dir = os.path.join(sdir, "bmc")
        os.makedirs(bmc_dir, exist_ok=True)
        for sec in secrets["bmc"]:
            dump_yaml("secret", sec, os.path.join(bmc_dir, f"{sec}.yaml"), cluster_ns)

    return {
        "aci": aci_path,
        "admin_kubeconfig": admin_kcfg_path,
        "kubeadmin_secrets_dir": ka_dir,
        "backup_dir": backup_dir,
    }


def strip_owner_refs_specific(paths):
    """
    Clean ownerReferences ONLY from:
      - AgentClusterInstall.yaml
      - AdminKubeconfigSecret.yaml
      - all kubeadmincreds/*.yaml
      - all bmc/*.yaml
    """
    print("\n=== Removing ownerReferences ONLY from required resources ===")

    # AgentClusterInstall
    print(f"Cleaning ACI → {paths['aci']}")
    run_cmd(["yq", "e", "-i", "del(.metadata.ownerReferences)", paths["aci"]])

    # Admin kubeconfig secret
    print(f"Cleaning Admin kubeconfig → {paths['admin_kubeconfig']}")
    run_cmd(["yq", "e", "-i", "del(.metadata.ownerReferences)", paths["admin_kubeconfig"]])

    # Kubeadmin password secrets
    kubeadmin_dir = paths["kubeadmin_secrets_dir"]
    print(f"Cleaning kubeadmin password secrets in {kubeadmin_dir}")
    for f in os.listdir(kubeadmin_dir):
        if f.endswith(".yaml"):
            fullpath = os.path.join(kubeadmin_dir, f)
            print(f"  → {fullpath}")
            run_cmd(["yq", "e", "-i", "del(.metadata.ownerReferences)", fullpath])

    # BMC secrets
    bmc_dir = os.path.join(paths["backup_dir"], "secrets", "bmc")
    if os.path.isdir(bmc_dir):
        print(f"Cleaning BMC secrets in {bmc_dir}")
        for f in os.listdir(bmc_dir):
            if f.endswith(".yaml"):
                fullpath = os.path.join(bmc_dir, f)
                print(f"  → {fullpath}")
                run_cmd(["yq", "e", "-i", "del(.metadata.ownerReferences)", fullpath])
    else:
        print("No BMC secrets found; skipping BMC cleanup.")

    print("\nOwnerReference cleanup complete.\n")


def print_manual_commands(cluster, ns):
    print("\n==============================================")
    print(" Run these commands manually after verification")
    print("==============================================\n")

    print("# A. Enable preserveOnDelete on ClusterDeployment")
    print(f"oc -n {ns} patch clusterdeployment {cluster} "
          "--type=merge -p '{\"spec\":{\"preserveOnDelete\":true}}'\n")

    print("# B. Delete ManagedCluster")
    print(f"oc delete managedcluster {cluster}\n")


    print(f"Run the above commands on the source ACM hub cluster\n")
# --------------------------------------------------------------------------
# RESTORE-SIDE HELPERS
# --------------------------------------------------------------------------

def load_yaml(path):
    """Load multi-document YAML file."""
    with open(path) as f:
        return list(yaml.safe_load_all(f))


def write_yaml(path, docs):
    """Write multi-document YAML file."""
    with open(path, "w") as f:
        yaml.dump_all(docs, f)


def apply_yaml(path):
    """oc apply -f path"""
    print(f"  → oc apply -f {path}")
    run_cmd(["oc", "apply", "-f", path])


_created_namespaces = set()


def ensure_namespace_exists(ns):
    """Create namespace if it doesn't exist."""
    if not ns or ns in _created_namespaces:
        return
    res = run_cmd(["oc", "get", "ns", ns], check=False)
    if res.returncode != 0:
        print(f"Creating namespace '{ns}'")
        run_cmd(["oc", "create", "namespace", ns])
    _created_namespaces.add(ns)


def apply_yaml_preserve_ns(path):
    """
    Load YAML, ensure all referenced namespaces exist, then apply as-is.
    Does NOT rewrite namespaces.
    """
    docs = load_yaml(path)
    for d in docs:
        if isinstance(d, dict):
            ns = d.get("metadata", {}).get("namespace")
            if ns:
                ensure_namespace_exists(ns)
    tmp = path + ".tmp"
    write_yaml(tmp, docs)
    apply_yaml(tmp)
    os.remove(tmp)


def restore_dir(label, path):
    """Restore all YAMLs inside a directory, preserving namespaces."""
    if not os.path.isdir(path):
        print(f"Skipping {label}: directory not found ({path})")
        return

    files = sorted([f for f in os.listdir(path) if f.endswith(".yaml")])
    if not files:
        print(f"Skipping {label}: no YAML files present")
        return

    print(f"\nRestoring {label}:")

    for fname in files:
        full = os.path.join(path, fname)
        apply_yaml_preserve_ns(full)


# --------------------------------------------------------------------------
# RESTORE PRE-CHECKS (Option A - strict, BMH based on backup)
# --------------------------------------------------------------------------

def deployment_ready(namespace, name):
    """Return True if deployment exists and status.availableReplicas == spec.replicas > 0."""
    try:
        data = oc_get("deploy", name, namespace, required=True)
    except SystemExit:
        return False

    spec = data.get("spec", {})
    status = data.get("status", {})
    desired = spec.get("replicas", 0)
    available = status.get("availableReplicas", 0)
    return bool(desired) and available == desired


def require_deploy_ready(namespace, name):
    if not deployment_ready(namespace, name):
        print(f"ERROR: Deployment {name} in namespace {namespace} is not READY "
              f"(availableReplicas != replicas or missing).", file=sys.stderr)
        sys.exit(1)
    print(f"  OK: {namespace}/{name} deployment is READY.")


def restore_prechecks(backup_dir):
    """
    Strict checks before restore (Option A):
      - assisted-service + agentinstalladmission READY in multicluster-engine.
      - If BMH manifests exist in backup (manifests/baremetalhosts/*.yaml):
            require metal3, metal3-baremetal-operator, metal3-image-customization
            READY in openshift-machine-api.
    """
    print("\n=== Restore Pre-Checks ===")

    # Assisted Installer controllers (mandatory)
    print("Checking Assisted Installer deployments in namespace 'multicluster-engine'...")
    require_deploy_ready("multicluster-engine", "assisted-service")
    require_deploy_ready("multicluster-engine", "agentinstalladmission")

    # Determine if BMH manifests exist in backup
    bmh_dir = os.path.join(backup_dir, "manifests", "baremetalhosts")
    bmh_manifests = os.path.isdir(bmh_dir) and any(
        f.endswith(".yaml") for f in os.listdir(bmh_dir)
    )

    if bmh_manifests:
        print("BareMetalHost manifests detected in backup; checking metal3-related "
              "deployments in 'openshift-machine-api'...")
        require_deploy_ready("openshift-machine-api", "metal3")
        require_deploy_ready("openshift-machine-api", "metal3-baremetal-operator")
        require_deploy_ready("openshift-machine-api", "metal3-image-customization")
    else:
        print("No BareMetalHost manifests detected in backup; metal3 deployments "
              "are not required for restore.")


# --------------------------------------------------------------------------
# MODE-SPECIFIC ENTRYPOINTS
# --------------------------------------------------------------------------

def do_backup(cluster, output_dir):
    cluster_ns = cluster

    print("\n=== Preflight Checks (Backup) ===")
    require_binary("oc")
    require_binary("yq")
    run_cmd(["oc", "whoami"])

    print("\n=== Validations & Discovery ===")
    validate_clusterdeployment(cluster, cluster_ns)
    validate_managedcluster_available(cluster)
    validate_aci_kac(cluster, cluster_ns)
    secrets = validate_secrets(cluster, cluster_ns)

    infraenvs = discover_infraenvs_for_cluster(cluster, cluster_ns)
    validate_bmh_states(infraenvs)

    print("\n=== Backup ===")
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.abspath(output_dir or f"{cluster}_backup_{timestamp}")

    paths = backup_all(cluster, cluster_ns, backup_dir, infraenvs, secrets)

    print("\n=== Limited YQ Cleanup ===")
    strip_owner_refs_specific(paths)

    print("\n=== Manual Commands ===")
    print_manual_commands(cluster, cluster_ns)

    print(f"\nBackup complete → {backup_dir}\n")


def do_restore(cluster, backup_dir):
    backup = os.path.abspath(backup_dir)

    print("\n=== Preflight Checks (Restore) ===")
    require_binary("oc")
    run_cmd(["oc", "whoami"])

    if not os.path.isdir(backup):
        print(f"ERROR: Backup directory does not exist: {backup}", file=sys.stderr)
        sys.exit(1)

    manifests = os.path.join(backup, "manifests")
    secrets = os.path.join(backup, "secrets")

    if not os.path.isdir(manifests) or not os.path.isdir(secrets):
        print("ERROR: Backup directory missing manifests/ or secrets/ subdirectories", file=sys.stderr)
        sys.exit(1)

    restore_prechecks(backup)

    # 1. Secrets
    print("\n=== Restoring Secrets ===")
    admin_kubeconfig_path = os.path.join(secrets, "AdminKubeconfigSecret.yaml")
    apply_yaml_preserve_ns(admin_kubeconfig_path)
    restore_dir("kubeadmin credentials", os.path.join(secrets, "kubeadmincreds"))
    restore_dir("pull secrets", os.path.join(secrets, "pullsecrets"))
    restore_dir("BMC secrets (optional)", os.path.join(secrets, "bmc"))

    # 2. Core CRs
    print("\n=== Restoring Core CRs ===")
    core_files = [
        ("ClusterDeployment", "ClusterDeployment.yaml"),
        ("AgentClusterInstall", "AgentClusterInstall.yaml"),
        ("KlusterletAddonConfig", "KlusterletAddonConfig.yaml"),
    ]
    for label, fname in core_files:
        path = os.path.join(manifests, fname)
        if not os.path.exists(path):
            print(f"Skipping {label}: file not found ({path})")
            continue
        print(f"\nRestoring {label}...")
        apply_yaml_preserve_ns(path)

    print("\nRestoring ManagedCluster...")
    mc_path = os.path.join(manifests, "ManagedCluster.yaml")
    apply_yaml_preserve_ns(mc_path)

    # 3. InfraEnvs, Agents, Nmstate, BMH
    restore_dir("InfraEnvs", os.path.join(manifests, "infraenvs"))
    restore_dir("Agent CRs", os.path.join(manifests, "agents"))
    restore_dir("NmstateConfigs (optional)", os.path.join(manifests, "nmstateconfigs"))
    restore_dir("BareMetalHosts (optional)", os.path.join(manifests, "baremetalhosts"))

    print("\n============================================================")
    print(" RESTORE COMPLETE")
    print("============================================================\n")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backup and restore Assisted Installer cluster resources on ACM "
                    "including multi-namespace InfraEnvs."
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["backup", "restore"],
        help="Operation mode: backup or restore",
    )
    parser.add_argument(
        "--cluster",
        required=True,
        help="Cluster name (also used as ClusterDeployment namespace/name)",
    )
    parser.add_argument(
        "--output-dir",
        help="Backup output directory (for backup mode)",
    )
    parser.add_argument(
        "--backup-dir",
        help="Existing backup directory (for restore mode)",
    )

    args = parser.parse_args()

    if args.mode == "backup":
        do_backup(args.cluster, args.output_dir)
    elif args.mode == "restore":
        if not args.backup_dir:
            print("ERROR: --backup-dir is required in restore mode", file=sys.stderr)
            sys.exit(1)
        do_restore(args.cluster, args.backup_dir)
    else:
        print("Unknown mode", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

