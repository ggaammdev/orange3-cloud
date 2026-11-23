# Azure checklist

Manual steps to check the whole add-on on a real Azure subscription: setup, a machine, the demo workflow, the idle shutdown and the cleanup. Run them before a release, and after changes to the Azure provider, the machine image or the bootstrap.

Use a subscription meant for testing, with a budget alert and vCPU quotas of at least 4 for "Standard Dsv6 Family vCPUs" and "Total Regional vCPUs" in the region ([setup-azure.md](setup-azure.md)). The Small machine (`Standard_D4s_v6`, about $0.23 an hour in `eastus` with its disk and address) is enough, and one pass costs a few cents.

Steps 1–5 are also automated, without the windows:

- `TestRealAzureDemo` in `tests/test_demo.py` opens the workflow as Orange's canvas does, starts a Small machine from the Cloud settings dialog with a 3-minute idle timeout, waits for every widget (Cloud File uploads the data to the storage account), then waits for the machine to stop by itself while Orange is open, and checks that the VM deallocated itself before the session deleted it, and that nothing of the machine is left (about 8 minutes);
- `test_real_machine_deallocates_itself` in `tests/test_azure_provider.py` starts a Small machine and closes the session, as when Orange quits, waits until the VM is `deallocated` by itself and checks that it stays so, then opens a new session, which finds it stopped, deletes it and records its cost (running, then its disk and address while deallocated; about 5 minutes);
- `test_real_machine` there starts a Small machine, checks the network rule, the machine's role and reattaching, then Stops it and checks that nothing of it is left (about 3 minutes).

```sh
set -a; source .orange3-cloud-dev.env; set +a
ORANGE3_CLOUD_AZURE_VM_TESTS=1 .venv/bin/pytest tests/test_demo.py -k RealAzure -s
ORANGE3_CLOUD_AZURE_VM_TESTS=1 .venv/bin/pytest tests/test_azure_provider.py -k real -s
```

## Before you start

- Orange with the add-on from a source checkout (`scripts/dev-setup.sh`), and the Azure CLI, signed in (`az login`).
- A development version uses the private development image `:dev` from the Artifact Registry of the Google Cloud project chosen on the Google Cloud page, pulled with a short-lived token of `orange3-cloud-image-reader` that your Google sign-in makes (see "Azure machines" and "Development images" in [dev.md](dev.md)). Make sure the image of the commit you test has been pushed, or set `ORANGE3_CLOUD_AZURE_IMAGE` to a `:dev-<sha>` tag.
- The demo data: `.venv/bin/python scripts/make-demo-data.py` writes `examples/demo-data.csv.gz` (1,000,000 rows, about 24 MB).
- Keep these commands at hand to see what is in the subscription (put the subscription's ID in `SUBSCRIPTION`; the CLI's Python may print warnings, which `PYTHONWARNINGS=ignore` hides):

  ```sh
  export PYTHONWARNINGS=ignore
  az vm list --subscription $SUBSCRIPTION -g orange3-cloud -d --query '[].[name,powerState]' -o tsv
  az disk list --subscription $SUBSCRIPTION -g orange3-cloud --query '[].name' -o tsv
  az network nic list --subscription $SUBSCRIPTION -g orange3-cloud --query '[].name' -o tsv
  az network public-ip list --subscription $SUBSCRIPTION -g orange3-cloud --query '[].[name,ipAddress]' -o tsv
  az network nsg list --subscription $SUBSCRIPTION -g orange3-cloud --query '[].name' -o tsv
  az role assignment list --subscription $SUBSCRIPTION --all -o tsv \
      --query "[?contains(scope, '/virtualMachines/orange3-cloud-')].[roleDefinitionName,scope]"
  ```

  All six should list nothing before you start. Read each kind of resource as above: Azure's `az resource list` may show deleted resources for a minute or two.

## 1. Setup

1. `scripts/run-orange.sh examples/local-demo.ows`. The widgets say they aren't connected to a cloud machine.
2. Click "Cloud settings…" in any Cloud widget and choose **Azure**.
   - [ ] The page shows the Azure CLI's path and your account. Without a sign-in, "Sign in to Azure" opens Microsoft's sign-in page in the browser and the account appears afterwards. While it waits, "Cancel sign-in" stops it.
   - [ ] "Subscription" lists the subscriptions of your sign-in; choose the test subscription. "Region" lists the regions; choose the test region.
   - [ ] "Machine size" lists the sizes the subscription may use in the region, each with an hourly price, and the prices' date. Choose **Small (testing)**.
   - [ ] The setup check passes (one line). With a size bigger than the quotas allow, a quota check fails and says how many vCPUs to request, and Start is off.
3. Set "Stop when idle for" to **5 min**.

## 2. Start

1. Click **Start**.
   - [ ] The dialog shows the machine's progress: the resource group, the network security group, creating the machine, allowing it to deallocate itself, booting, installing Python and your packages, starting the server. Azure shows the serial log a minute or two late, so "Booting the machine" may stay for most of the start.
   - [ ] It connects in one and a half to two minutes. "Price" shows about $0.23/h, and "Stops in" counts down the idle time.
2. In the portal or with the commands above:
   - [ ] one VM `orange3-cloud-<id>`, `Standard_D4s_v6`, Ubuntu 26.04 LTS, tags `orange3-cloud=true` and `orange3-cloud-session=<id>`, a system-assigned identity; its disk, network interface and public IP address have the same name prefix and are deleted with the VM,
   - [ ] the VM's identity has the role "Desktop Virtualization Power On Off Contributor" on the VM only,
   - [ ] the network security group `orange3-cloud-https` has one inbound rule of its own: TCP 8443 only from your public IP address (`/32`).

## 3. The demo workflow

Cloud File starts as soon as the machine is connected, and the rest of the workflow follows by itself.

- [ ] **Cloud File:** the first time, it asks before creating the storage account and giving you its role. It uploads the file to the storage account (with progress), and the machine copies it from there and loads 1,000,000 rows. Loading the workflow again doesn't upload the file again.
- [ ] **Cloud Test & Score:** 5-fold cross-validation of Random Forest and Logistic Regression on all rows. AUC about 0.83 and 0.77. The status line says how long it took and what it cost ("Cost about $… at $0.23/h.").
- [ ] **ROC Analysis** (Orange's own widget) draws both curves.
- [ ] **Cloud Learner** trains Logistic Regression on the machine, and **Cloud Predictions** predicts all rows.
- [ ] **Cloud Data Table** shows 1,000,000 rows with the predictions; paging works.
- [ ] No widget shows an error. "Cost so far" in the dialog has grown, with the jobs' share in brackets.

## 4. Idle shutdown

Leave the workflow alone. Don't click anything in the Cloud widgets.

- [ ] Within about a minute after the idle timeout has passed since the last job, the dialog says "The machine stopped: it was idle for longer than the idle timeout." and the widgets say they aren't connected.
- [ ] The VM first deallocated itself, then the add-on deleted it. The resource group's **Activity log** in the portal has "Deallocate Virtual Machine" by the VM's own identity (shown as an ID, not your name), then "Delete Virtual Machine" by you, a few seconds apart. The same from the command line (Azure lists an event a few minutes late):

  ```sh
  az monitor activity-log list --subscription $SUBSCRIPTION -g orange3-cloud --offset 1h --max-events 1000 -o tsv \
      --query "[?ends_with(operationName.value, 'virtualMachines/deallocate/action') || ends_with(operationName.value, 'virtualMachines/delete')].[eventTimestamp,operationName.value,status.value,caller]"
  ```

- [ ] The commands above list nothing.

## 5. Closed Orange, and Stop

1. Start the machine again and wait until Cloud File has loaded.
2. Close Orange and open it again with the workflow.
   - [ ] It reconnects to the same machine, and the widgets work without starting a new one.
3. Close Orange and wait for the idle timeout and a minute more.
   - [ ] The VM is `VM deallocated`. Its disk, network interface and public IP address are still there (the disk and the address are billed).
4. Open Orange again.
   - [ ] The dialog says "The machine stopped." The commands list nothing: Orange deleted the deallocated VM with its disk, interface and address, its role, and the network security group.
5. Start a machine again, then click **Stop**.
   - [ ] The dialog says the machine is stopped within about a minute, and records its cost.
   - [ ] The commands list nothing.

## 6. Clean up

- [ ] In Cloud File, "Uploaded files…" lists the demo file; delete it. The storage account stays, empty, for next time.
- [ ] The commands list nothing. `az resource list --subscription $SUBSCRIPTION -g orange3-cloud -o table` shows only the virtual network `orange3-cloud` and the storage account `o3cloud…`, and the role assignments in the group only your "Storage Blob Data Contributor" on the storage account.

## What to expect

Measured on a Small machine in `eastus`, with the demo's 1,000,000 rows:

| Step | Time |
|---|---|
| Start to connected | 1.5–2 min: the network pieces, the VM and its role ~15 s, then booting, installing Docker, downloading the machine image and installing Python and the packages |
| Cloud File (upload and load) | under 30 s, most of it the upload of 24 MB; the machine loads the file in about 1 s |
| Cloud Test & Score | about 1 min on the machine (both learners, 5 folds, all rows), plus under a minute to download the 61 MB of results |
| Cloud Learner and Cloud Predictions | about 1 s and 4 s |
| Whole workflow after connecting | a little over 2 min |
| Idle shutdown noticed (Orange open) | within a minute after the idle timeout; the VM deallocates itself in about 15 s, and Orange deletes it right after |
| VM `deallocated` (Orange closed) | about 15 s after the idle timeout |
| Stop | under a minute |
| Cost of one pass | about $0.03 |
