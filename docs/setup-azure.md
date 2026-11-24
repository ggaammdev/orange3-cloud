# Setting up Azure

The add-on can start machines in your own Azure subscription. This guide sets up the subscription once. It takes about 15 minutes, plus the wait if you need a higher vCPU quota. Orange checks the subscription for you and says what is still missing, so you can also start at step 4 and come back here when the check asks for something.

You need:

- an Azure subscription, with a payment method,
- Orange with the `orange3-cloud` add-on (see the [README](../README.md)).

## 1. A subscription and your role in it

1. If you don't have an Azure subscription, create one at [azure.microsoft.com](https://azure.microsoft.com) (**Try Azure for free** or **Pay as you go**). Microsoft asks for a payment method and verifies your phone number.
2. **Free trial subscriptions** have small vCPU quotas, and Azure doesn't raise them: only the Small machine size may fit. The setup check says so when a size doesn't fit. To run bigger sizes, upgrade the subscription to pay-as-you-go in the [Azure portal](https://portal.azure.com) (**Subscriptions**, your subscription, **Upgrade**). A free trial also stops its resources when its credit runs out.
3. **Your role.** In a subscription you created, you are its **Owner**, which is all the add-on needs. Otherwise, ask the subscription's owner for:
   - **Contributor**, to create the machines, the network and the storage account, and to register the resource providers (step 5),
   - and **User Access Administrator** or **Role Based Access Control Administrator**, to give roles. Contributor alone can't. The add-on gives two roles:
     - each machine's own identity gets the role "Desktop Virtualization Power On Off Contributor" on its own VM, and nothing else, so that the machine can deallocate itself when it stops (step 9),
     - you get "Storage Blob Data Contributor" on the add-on's storage account, to upload files and let the machine read them (step 8). The data in a storage account needs a role of its own, even for the subscription's Owner.

   Give the roles on the subscription. A user with Owner, or Contributor plus one of the other two, can give them in the portal: **Subscriptions**, the subscription, **Access control (IAM)**, **Add role assignment**.

The add-on creates everything in one resource group, `orange3-cloud`, in the region you choose: a virtual network, a network security group, a storage account for your files, and the machines. It creates nothing outside it.

## 2. A budget alert

The add-on stops idle machines and shows what each job cost, but it has no spending limit of its own. A budget alert emails you when the month's costs reach an amount you choose:

1. In the portal, open your subscription, then **Budgets** (under **Cost Management**) and **Add**.
2. Choose an amount (for example $20 a month), and add alert conditions (for example at 50%, 90% and 100% of the actual cost) with your email address.

An alert doesn't stop anything: it only sends an email.

## 3. The Azure CLI

The add-on signs in to Azure with Microsoft's `az` command-line tool.

1. Install it as [Microsoft's instructions](https://learn.microsoft.com/cli/azure/install-azure-cli) describe. On a Mac with Homebrew, `brew install azure-cli` works too.
2. You don't need to run `az login` or `az configure` yourself. Orange finds `az` in the usual install folders and on your shell's `PATH`, even when Orange is started from the Dock.

## 4. Sign in from Orange

1. Open any Cloud widget, for example Cloud File, and click **Cloud settings…**.
2. Choose **Azure**.
3. Click **Sign in to Azure**. Your browser opens Microsoft's sign-in page. Sign in, with your organization's second factor if it asks for one. Back in Orange, the dialog shows your account.

   This runs `az login`, which keeps the sign-in on your computer, where Azure's tools keep it (`~/.azure`). The add-on never sees your password. When the sign-in expires, the dialog says so: click **Sign in again**.

4. Choose the **subscription**. The list has the subscriptions of your sign-in, in every directory you belong to. The first time, it is the Azure CLI's default subscription. After you were given access to another subscription, click **Reload** (or sign in again).
5. Choose a **region** near you or near your data, for example `eastus`, `westeurope` or `southeastasia`. The first time, it is the Azure CLI's default location if you set one (`az config set defaults.location=westeurope`), otherwise East US. Prices, and the sizes a subscription may use, differ between regions.

## 5. The setup check

Below the region, the dialog checks everything a machine needs and shows what is missing, with a link to the right page of the portal. When everything passes, it shows one line, "The Azure subscription is ready".

| Check | What to do if it fails |
|---|---|
| Signed in to Azure | Click **Sign in to Azure** (step 4). |
| Subscription enabled | Choose another subscription, or check its billing in the portal. A disabled subscription, for example a free trial whose credit ran out, can't start machines. |
| Resource providers registered | A subscription must register `Microsoft.Compute`, `Microsoft.Network` and `Microsoft.Storage` before it uses them for the first time. Click **Register…**: Orange asks first, then registers them, which takes a minute or two. |
| Permissions for machines, networks and storage | You may not create VMs, networks or storage accounts. Ask for the Contributor role (step 1). |
| Permission to give machines their role | You may not give roles. Ask for User Access Administrator or Role Based Access Control Administrator, or Owner (step 1). |
| vCPU quota of the size's family in the region | The D sizes count against "Standard Dsv6 Family vCPUs", the high-memory E sizes against "Standard Esv6 Family vCPUs". Request more (see below), choose a smaller size, or stop other VMs. |
| Total vCPU quota in the region | All VMs in a region count against "Total Regional vCPUs". Request more (see below). |

**Requesting more quota:** your running VMs may use only so many vCPUs in a region, per family and in all. New subscriptions often have about 10 per family. The check links to **Quotas** in the portal: choose **Compute**, filter by your subscription and region, select the quota the check names, and request at least the number of vCPUs it says. Small needs 4, Medium 16, Large 32. Request the family's quota and the regional total if both fail. Azure approves many requests within minutes. When it can't, it asks you to open a support request, which takes longer. While a request is open, the check says so. Free trial subscriptions can't get more (step 1).

## 6. Machine size and limits

**Machine size** lists the sizes your subscription may use in the region, with their hourly list price there. Cloud File suggests a size that fits the file you load ("fits your data").

| Size | VM size | vCPUs | Memory | Price in `eastus` |
|---|---|---:|---:|---:|
| Small (testing) | `Standard_D4s_v6` | 4 | 16 GiB | $0.23/h |
| Medium (default) | `Standard_D16s_v6` | 16 | 64 GiB | $0.84/h |
| Medium, high memory | `Standard_E16s_v6` | 16 | 128 GiB | $1.09/h |
| Large | `Standard_D32s_v6` | 32 | 128 GiB | $1.64/h |
| Large, high memory | `Standard_E32s_v6` | 32 | 256 GiB | $2.15/h |
| Extra large | `Standard_D64s_v6` | 64 | 256 GiB | $3.26/h |
| Extra large, high memory | `Standard_E64s_v6` | 64 | 512 GiB | $4.27/h |
| Huge | `Standard_D96s_v6` | 96 | 384 GiB | $4.87/h |
| Huge, high memory | `Standard_E96s_v6` | 96 | 768 GiB | $6.38/h |

The prices are Azure's pay-as-you-go list prices of November 2026 for Linux, with a 100 GB Premium SSD OS disk and the machine's public IP address included. Reservations, savings plans, free amounts, network traffic and storage aren't included.

As a rule of thumb, a table needs about 8 bytes per value in memory (rows × columns × 8), and learning needs several times that.

- **Boot disk:** 100 GB by default, from 50 to 2000 GB. The disk holds the files the machine loads (it copies files from storage to its disk first), Python and your packages. Make it larger for big files. Azure bills Premium SSD disks by size steps (64, 128, 256, 512, 1024 and 2048 GiB), so a 100 GB disk costs as much as one of 128 GiB, about $20 a month.
- **Stop when idle for:** 30 minutes by default. The machine stops when no job has run and no Cloud widget has asked it for anything for this long. If it hasn't heard from Orange at all for this long, for example because Orange crashed or the laptop slept, it also cancels its jobs, which nobody is waiting for.
- **Stop after at most:** 12 hours by default, even while it works.

These apply to the next machine you start.

## 7. Start a machine

Click **Start**. The dialog shows the steps:

1. creating the resource group `orange3-cloud`, the virtual network `orange3-cloud` and the network security group `orange3-cloud-https` the first time, and allowing your computer's IP address through the security group,
2. creating the machine `orange3-cloud-<id>` (a VM with its disk, network interface and public IP address),
3. allowing the machine to deallocate itself (its role),
4. booting and installing Docker, then installing Python, Orange and your packages at your exact versions,
5. starting the server.

It usually takes one and a half to two minutes. Azure shows the machine's progress a minute or two late, so the dialog may say "Booting the machine" for most of that time. Then the Cloud widgets say "Connected", and the dialog shows the machine's price, its cost so far and the time until it stops.

When you quit Orange while the machine runs, the Cloud widgets cancel their jobs, and Orange asks what to do with the machine: **Stop and quit** stops it first, **Keep it running** leaves it running until its idle time is over, and **Cancel** keeps Orange open. When you open Orange again while it runs, Orange connects to it again.

## 8. Your files in the storage account

When Cloud File loads a file from your computer for the first time, it asks to create a storage account in the resource group, `o3cloud<random>`, in the machine's region, and to give you the role "Storage Blob Data Contributor" on it. Azure takes a minute or two to apply a new role, so the first upload may wait for it, and Cloud File says so. The account is private: public access and account keys are off, and every request uses your sign-in. Cloud File uploads the file to its container `orange3-cloud` in blocks, and an upload continues where it stopped if the connection breaks.

Files stay in the storage account, so loading the same file in a later session doesn't upload it again. Storage costs about $0.021 per GB a month in `eastus`. **Uploaded files…** in Cloud File lists them and deletes the ones you check. Deleting a file doesn't affect tables already loaded on a running machine.

The machine has no access to your storage of its own. For each load, the add-on signs a read-only link to the file with your sign-in, valid for an hour, and the machine copies the file to its disk through it before reading it. So files in your own storage accounts load the same way, as `az://<storage account>/<container>/<path>`, as long as your sign-in may read them (for example with "Storage Blob Data Reader" on the account). Files at `https://` URLs are loaded by the machine directly. Reading a storage account in the machine's region costs nothing; from another region, Azure charges for the transfer.

## 9. Stop, and check that nothing is left

- **Stop machine** in any Cloud widget, or **Stop** in Cloud settings, deallocates the machine, deletes its role, deletes the VM with its disk, network interface and public IP address, and deletes the network security group when no other machine is left. The resource group, the virtual network and the storage account stay for next time; the first two cost nothing.
- A machine also stops by itself after the idle time or the maximum lifetime: it **deallocates** its own VM, so its compute costs nothing any more. If Orange is open then, Orange deletes it. If Orange was closed, the VM stays deallocated, and its disk and public IP address still cost (about $0.03 an hour together with a 100 GB disk, or $23 a month) until you open Orange again, which deletes it.
- If Orange quits while a machine is starting, the machine may be left behind. Cloud settings lists such machines with a **Delete them…** button.

To check yourself, open **Resource groups → orange3-cloud** in the portal, or run (with your subscription's ID or name):

```sh
az vm list --subscription SUBSCRIPTION -g orange3-cloud -d --query '[].[name,powerState]' -o tsv
az resource list --subscription SUBSCRIPTION -g orange3-cloud --query '[].[type,name]' -o tsv
az role assignment list --subscription SUBSCRIPTION --all -o tsv \
    --query "[?contains(scope, '/resourceGroups/orange3-cloud/')].[roleDefinitionName,scope]"
```

With no machine, the first lists nothing, the second only the virtual network and the storage account, and the third only your role on the storage account. Azure's list of resources may show a deleted resource for a minute or two. All the add-on's resources have the tag `orange3-cloud=true`. To see their costs, open **Cost Management → Cost analysis** and filter by the resource group `orange3-cloud`.

## Troubleshooting

- **"Azure CLI: Not found"**: install it (step 3) and click **Reload**. If it is installed in an unusual place, start Orange from a terminal where `az` works.
- **"Not signed in"** or a sign-in that stopped working: click **Sign in to Azure** (or **Sign in again**).
- **A subscription is missing from the list**: sign in with the account that has access to it, then click **Reload**.
- **A quota check fails**: choose a smaller size or another region, or request more quota (step 5).
- **Start fails because Azure has no capacity for the size** in the region at the moment: try again later, or choose another size or region.
- **Start fails while giving the machine its role**: you may not give roles (step 1).
- **The machine doesn't connect**: your IP address may have changed, or a VPN or company network may block port 8443. The add-on updates the network security group when your address changes; try again after leaving the VPN. The dialog's messages show where the start stopped.
- **"Not enough memory"** or a warning in Cloud File: choose a larger size. Cloud File's estimate suggests one.
- **A file in storage doesn't load**: Cloud File says whether it doesn't exist or your sign-in may not read it. The machine needs room on its disk for a copy of the file; make the boot disk larger for very large files.
- **A package failed to install on the machine**: Cloud settings lists the versions on your computer and on the machine, and the packages that couldn't be installed. Packages installed from a folder on your computer can't be copied.

## Remove everything

To remove everything the add-on created in your subscription:

1. Click **Stop** in Cloud settings, and **Delete them…** if it lists machines left behind.
2. Delete your uploaded files with **Uploaded files…** in Cloud File.
3. To remove the rest, delete the resource group `orange3-cloud` in the portal (**Resource groups → orange3-cloud → Delete resource group**), or with `az group delete --subscription SUBSCRIPTION -n orange3-cloud`. This deletes the virtual network, the storage account with any files still in it, and your role on it.
