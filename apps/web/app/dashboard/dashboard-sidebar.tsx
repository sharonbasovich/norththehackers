"use client";

import {
  IconBook2,
  IconHome2,
  IconArrowLeft,
} from "@tabler/icons-react";
import { TrivialityLogo } from "@/components/triviality-logo";
import {
  Sidebar,
  SidebarBody,
  SidebarLink,
  useSidebar,
} from "@/components/ui/sidebar";

const links = [
  { label: "Overview", href: "/dashboard", icon: <IconHome2 size={20} /> },
  { label: "Literature", href: "/dashboard/literature", icon: <IconBook2 size={20} /> },
];

export function DashboardSidebar() {
  return (
    <Sidebar>
      <SidebarBody className="overflow-hidden border-r border-black/10 !bg-[#f5f5f5] text-[#111] md:sticky md:top-0 md:!min-h-screen md:!h-screen">
        <div className="flex flex-1 flex-col gap-10">
          <DashboardBrand />
          <div className="flex flex-col gap-3">
            {links.map((link) => (
              <SidebarLink key={link.label} link={link} />
            ))}
          </div>
        </div>

        <div className="flex flex-col gap-3">
          <SidebarLink
            link={{
              label: "Home",
              href: "/",
              icon: <IconArrowLeft size={20} />,
            }}
          />
        </div>
      </SidebarBody>
    </Sidebar>
  );
}

function DashboardBrand() {
  const { open, animate } = useSidebar();

  return (
    <div className="flex h-10 items-center overflow-hidden">
      <TrivialityLogo wordmark={!animate || open} />
    </div>
  );
}
